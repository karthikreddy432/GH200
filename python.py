#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import http.client
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
import urllib3
from requests.adapters import HTTPAdapter

GITHUB_API = "https://api.github.com"

# Parse the workflow URL stored in the ServiceNow description, or a deployment status
# log_url/target_url. Attempt number is optional (not all URLs include it).
GITHUB_RUN_URL_PATTERN = re.compile(
    r"https://github\.com/(?P<repository>[^/\s]+/[^/\s]+)/actions/runs/(?P<run_id>\d+)(?:/attempts/(?P<attempt>\d+))?"
)

# Flow: ServiceNow unsuccessful CHGs (only to discover repository names) -> GitHub Deployments API
# (PRD, date range) -> failed deployments -> workflow run -> failed jobs -> failed job logs -> CHG + RCA.

# The change number is echoed into the deployment workflow's job logs (e.g. by the get-cr-number /
# check-snow-cr-status actions), so it is recovered from the failed attempt's own run.
CHANGE_NUMBER_PATTERN = re.compile(r"\bCHG\d{7,}\b")

MAX_LOG_EVIDENCE_LENGTH = 1500
CONTEXT_LINES_BEFORE = 5
CONTEXT_LINES_AFTER = 10


def _ora_description(evidence: str) -> str:
    match = re.search(r"ORA-\d+[^\n]*", evidence, re.IGNORECASE)
    code = match.group(0).strip() if match else "an ORA error"
    return f"Oracle database returned {code} during the deployment."


def _liquibase_description(evidence: str) -> str:
    changeset_match = re.search(r"changeset[:\s]+([^\s,]+)", evidence, re.IGNORECASE)
    changeset = changeset_match.group(1) if changeset_match else "a changeSet"
    if re.search(r"already exists|attempted to create an existing object", evidence, re.IGNORECASE):
        return f"Liquibase failed because changeSet {changeset} attempted to create an existing object."
    if re.search(r"lock", evidence, re.IGNORECASE):
        return "Liquibase could not acquire the changelog lock; another process may be holding it."
    return f"Liquibase failed while applying changeSet {changeset}."


def _invalid_commit_description(evidence: str) -> str:
    ref_match = re.search(r"pathspec '([^']+)'", evidence, re.IGNORECASE)
    ref = ref_match.group(1) if ref_match else "the requested commit/tag"
    return (
        f"Deployment failed because git checkout target '{ref}' does not exist in the repository "
        "(invalid or stale commit/tag/version input)."
    )


# Root-cause signals, ranked by priority (deeper/more specific causes win over generic symptoms).
# Each rule: category, priority (strategic rank, higher overrides lower), patterns (list of
# {regex, description}; description is a static sentence or a callable(evidence) -> sentence),
# action (recommended remediation).
def _missing_placeholder_description(evidence: str) -> str:
    # Only "- ***NAME***" bullet lines list the actually-missing placeholders; "Replacing
    # variable ***NAME***" lines nearby refer to ones that were resolved successfully.
    names = list(dict.fromkeys(re.findall(r"^\s*(?:\ufeff?\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*)?-\s*\*\*\*([A-Za-z0-9_.\-]+)\*\*\*", evidence, re.MULTILINE)))
    if names:
        return f"Deployment failed because required CI/CD variables/secrets were not defined: {', '.join(names)}."
    return "Deployment failed because one or more required CI/CD variables/secrets were not defined for placeholder replacement."


# Checked FIRST in the failed-job logs, before the workflow failure summary, the Kubernetes rules and
# every other rule: placeholder replacement failed because variables/secrets are not defined.
MISSING_VARIABLES_RULE = {
    "category": "Missing CI/CD variable or secret definition",
    "priority": 99,
    "patterns": (
        {"regex": r"unreplaced placeholders", "description": _missing_placeholder_description},
        {"regex": r"PLACEHOLDER REPLACEMENT FAILED", "description": _missing_placeholder_description},
        {"regex": r"Missing variables/secrets that need to be defined", "description": _missing_placeholder_description},
    ),
    "action": "Define the missing variables/secrets (with normalized names) in Tech Central for this component's CI/CD configuration, then retry the deployment.",
}


ROOT_CAUSE_RULES = (
    {
        "category": "Credential / authentication issue",
        "priority": 98,
        "patterns": (
            {"regex": r"invalid_client", "description": "PingFederate client authentication failed due to an invalid_client response."},
            {"regex": r"401 unauthorized", "description": "The target service returned HTTP 401 Unauthorized for the deployment request."},
            {"regex": r"unauthorized", "description": "The target service rejected the deployment request as unauthorized."},
            {"regex": r"authentication failed", "description": "Authentication failed against the target service using the configured credentials."},
            {"regex": r"access denied", "description": "Access was denied by the target service for the configured credentials/role."},
            {"regex": r"invalid credentials", "description": "The configured credentials were rejected as invalid."},
            {"regex": r"token.*expired", "description": "The authentication token used by the deployment had expired."},
        ),
        "action": "Rotate or correct the referenced credentials/client configuration and validate target service authentication before retrying.",
    },
    {
        "category": "Missing Kubernetes configuration",
        "priority": 96,
        "patterns": (
            {"regex": r"secret.*not found", "description": "A required Kubernetes Secret referenced by the deployment was not found in the target namespace."},
            {"regex": r"configmap.*not found", "description": "A required Kubernetes ConfigMap referenced by the deployment was not found in the target namespace."},
            {"regex": r"failed to mount secret", "description": "The deployment failed to mount a required Secret volume."},
            {"regex": r"could not find secret", "description": "The deployment could not locate a required Secret."},
            {"regex": r"missing required.*(secret|config)", "description": "A required secret/config value was missing from the deployment configuration."},
        ),
        "action": "Verify the referenced Secret/ConfigMap exists in the target namespace and is mounted or injected under the expected name.",
    },
    {
        "category": "Invalid deployment commit/version reference",
        "priority": 97,
        "patterns": (
            {"regex": r"pathspec .* did not match any file\(s\) known to git", "description": _invalid_commit_description},
            {"regex": r"fatal: reference is not a tree", "description": "Deployment failed because the referenced commit/tag does not exist as a git tree (invalid or stale version input)."},
            {"regex": r"unknown revision or path not in the working tree", "description": "Deployment failed because the requested commit/tag/branch could not be resolved in the repository."},
        ),
        "action": "Verify the commit/tag/version being deployed exists in the source repository and correct the version input before retrying.",
    },
    {
        # CHANGED (item 5): ORA-01017 is a credential rejection, not a connectivity problem, so it
        # must outrank the generic ORA-\d+ rule in "Database connectivity issue" (priority 94).
        "category": "Database authorization issue",
        "priority": 95.5,
        "patterns": (
            {"regex": r"ORA-01017", "description": "Database authentication failed because the configured username/password was rejected (ORA-01017)."},
        ),
        "action": "Validate credentials, secrets, account status, and permissions before retrying.",
    },
    {
        "category": "Database authorization issue",
        "priority": 95,
        "patterns": (
            {"regex": r"password authentication failed", "description": "Database authentication failed due to an incorrect password for the configured account."},
            {"regex": r"login failed", "description": "Database login failed for the configured account."},
            {"regex": r"snowflake.*(authentication|authorization|access denied)", "description": "Snowflake rejected the connection due to an authentication/authorization failure."},
            {"regex": r"permission denied.*(database|schema|table)", "description": "The database denied the required schema/table permission for the deployment account."},
        ),
        "action": "Validate database credentials, account status, and the grants required for the migration/connection.",
    },
    {
        "category": "Database connectivity issue",
        "priority": 94,
        "patterns": (
            {"regex": r"ORA-\d+", "description": _ora_description},
            {"regex": r"snowflake.*(timeout|timed out)", "description": "Connection to Snowflake timed out while validating deployment changes."},
            {"regex": r"database.*(connection refused|timeout)", "description": "Connection to the database was refused or timed out during the deployment."},
            {"regex": r"could not connect to (the )?database", "description": "The deployment could not establish a connection to the database."},
            {"regex": r"sql exception", "description": "A SQL exception occurred during the deployment's database operation."},
            {"regex": r"database exception", "description": "A database exception occurred during the deployment."},
        ),
        "action": "Validate database endpoint reachability, network policy/firewall rules, and the exact database error code.",
    },
    {
        "category": "Container image issue",
        "priority": 93,
        "patterns": (
            {"regex": r"ImagePullBackOff", "description": "The deployment could not pull the container image (ImagePullBackOff)."},
            {"regex": r"ErrImagePull", "description": "The deployment failed to pull the container image (ErrImagePull)."},
            {"regex": r"pull access denied", "description": "The container registry denied pull access for the image."},
            {"regex": r"manifest unknown", "description": "The requested image manifest/tag was not found in the registry."},
            {"regex": r"not found.*image", "description": "The requested container image was not found in the registry."},
        ),
        "action": "Verify the image tag, registry access, and pull secret, and confirm the image exists in the target registry.",
    },
    {
        "category": "Network or DNS connectivity issue",
        "priority": 92,
        "patterns": (
            {"regex": r"temporary failure in name resolution", "description": "DNS resolution failed (temporary failure in name resolution) for a required endpoint."},
            {"regex": r"name or service not known", "description": "DNS resolution failed (name or service not known) for a required endpoint."},
            {"regex": r"no such host", "description": "The deployment could not resolve a required host (no such host)."},
            {"regex": r"connection refused", "description": "Connection to a required endpoint was refused."},
            {"regex": r"connection timed out", "description": "Connection to a required endpoint timed out."},
            {"regex": r"networkpolicy", "description": "A NetworkPolicy blocked required traffic for the deployment."},
        ),
        "action": "Validate DNS, service endpoints, network policies/routes, and target port reachability from the failing workload.",
    },
    {
        "category": "Storage or persistent volume issue",
        "priority": 90,
        "patterns": (
            {"regex": r"FailedMount", "description": "The pod failed to mount a required volume (FailedMount)."},
            {"regex": r"FailedAttachVolume", "description": "The required persistent volume failed to attach to the node (FailedAttachVolume)."},
            {"regex": r"PersistentVolumeClaim.*(pending|failed)", "description": "The required PersistentVolumeClaim remained pending or failed to bind."},
            {"regex": r"MountVolume.*failed", "description": "Mounting the required volume failed (MountVolume.SetUp failed)."},
            {"regex": r"insufficient storage", "description": "The target node/storage class reported insufficient storage capacity."},
        ),
        "action": "Inspect PVC/PV state, storage class, mount events, and node attachment errors before retrying.",
    },
    {
        "category": "Kafka connectivity or authorization issue",
        "priority": 88,
        "patterns": (
            {"regex": r"kafka.*(authorization|authentication)", "description": "Kafka authorization failed for the deployment's client credentials."},
            {"regex": r"sasl authentication failed", "description": "Kafka SASL authentication failed for the configured credentials."},
            {"regex": r"kafka.*(timeout|connection)", "description": "The deployment could not connect to the Kafka broker (timeout or connection failure)."},
            {"regex": r"broker.*unavailable", "description": "The Kafka broker was unavailable during the deployment."},
            {"regex": r"not authorized to access topics", "description": "The deployment's client is not authorized to access the required Kafka topic(s)."},
        ),
        "action": "Validate broker reachability, SASL/TLS credentials, ACLs, topic existence, and consumer-group permissions.",
    },
    {
        "category": "Liquibase database migration issue",
        "priority": 85,
        "patterns": (
            {"regex": r"liquibaseexception", "description": _liquibase_description},
            {"regex": r"liquibase\.exception", "description": _liquibase_description},
            {"regex": r"changeset.*failed", "description": _liquibase_description},
            {"regex": r"lock.*(acquisition|exception).*liquibase", "description": _liquibase_description},
            {"regex": r"migration failed", "description": "The Liquibase migration failed to apply against the target database."},
            {"regex": r"schema validation failure", "description": "Liquibase schema validation failed against the target database."},
        ),
        "action": "Review the failing Liquibase changeSet and database response for the actual migration error before retrying.",
    },
    {
        "category": "Memory / OOM issue",
        "priority": 80,
        "patterns": (
            {"regex": r"OOMKilled", "description": "The container was OOMKilled after exceeding its memory limit."},
            {"regex": r"out of memory", "description": "The process ran out of memory during execution."},
            {"regex": r"exit code 137", "description": "The container exited with code 137 (OOMKilled)."},
            {"regex": r"exit code: 137", "description": "The container exited with code 137 (OOMKilled)."},
        ),
        "action": "Review container memory requests/limits and heap sizing; inspect the pod's previous logs and OOMKilled event.",
    },
    {
        "category": "Artifact missing in Nexus",
        "priority": 91.5,
        "patterns": (
            {"regex": r"could not find artifact", "description": "A required artifact could not be found in the Nexus repository."},
            {"regex": r"artifact.*(not found|does not exist)", "description": "The artifact to deploy was not found in the artifact repository."},
            {"regex": r"failed to (download|fetch|resolve) artifact", "description": "The deployment could not download/resolve the required artifact."},
        ),
        "action": "Confirm the artifact/version was published to Nexus (and the version input is correct) before retrying.",
    },
    {
        "category": "Artifact publish failure",
        "priority": 91,
        "patterns": (
            {"regex": r"failed to deploy artifacts", "description": "Publishing the build artifact to the repository failed."},
            {"regex": r"return code is: (400|401|403|409|413|500)", "description": "The artifact repository rejected the artifact upload."},
            {"regex": r"repository does not allow updating assets", "description": "The artifact repository does not allow overwriting an existing artifact version."},
            {"regex": r"(upload|publish).*(failed|error).*(nexus|artifact)", "description": "Uploading/publishing the artifact failed."},
            {"regex": r"(nexus|artifact).*(upload|publish).*(failed|error)", "description": "Uploading/publishing the artifact failed."},
        ),
        "action": "Check artifact repository permissions, version immutability rules and storage before retrying the publish.",
    },
    {
        "category": "Build failure",
        "priority": 75,
        "patterns": (
            {"regex": r"BUILD FAILURE", "description": "The Maven/Gradle build failed."},
            {"regex": r"COMPILATION ERROR", "description": "The build failed with a compilation error."},
            {"regex": r"failed to execute goal", "description": "A Maven goal failed during the build."},
            {"regex": r"npm ERR!", "description": "The npm build/install failed."},
            {"regex": r"compilation failed", "description": "The build failed to compile."},
            {"regex": r"build failed", "description": "The build step reported a failure."},
        ),
        "action": "Review the build error and failing step in the workflow logs; fix the source/dependency problem and rebuild.",
    },
    {
        "category": "Helm failure",
        "priority": 70,
        "patterns": (
            {"regex": r"UPGRADE FAILED", "description": "Helm upgrade failed."},
            {"regex": r"INSTALLATION FAILED", "description": "Helm install failed."},
            {"regex": r"rendered manifests contain a resource that already exists", "description": "Helm failed because a rendered resource already exists and is not managed by the release."},
            {"regex": r"another operation \(install/upgrade/rollback\) is in progress", "description": "Helm failed because another release operation is already in progress."},
            {"regex": r"helm.*(error|failed)", "description": "A Helm command failed during the deployment."},
        ),
        "action": "Inspect the Helm error, release status (helm status/history) and rendered manifests before retrying.",
    },
    {
        "category": "Deployment timeout",
        "priority": 60,
        "patterns": (
            {"regex": r"timed out waiting for the condition", "description": "The deployment timed out waiting for the workload to become ready."},
            {"regex": r"deployment.*timed out", "description": "The deployment timed out."},
        ),
        "action": "Check pod events and application logs for why the workload did not become ready in time.",
    },
    {
        # Fallback-only: a generic application exception is only reported when no deeper,
        # more specific root cause (credentials, DB, Kafka, image, storage, OOM, etc.) was found.
        "category": "Application startup failure",
        "priority": 10,
        "patterns": (
            {"regex": r"BeanCreationException", "description": "The application failed to start due to a BeanCreationException during initialization."},
            # CHANGED (item 6): the generic "Caused by:" signal was removed to avoid false positives.
            {"regex": r"startup exception", "description": "The application reported a startup exception."},
            {"regex": r"traceback \(most recent call last\)", "description": "The application raised an unhandled Python exception during startup."},
        ),
        "action": "Inspect the application exception and pod previous logs; correct the startup configuration before retrying the deployment.",
    },
    {
        # Lowest-priority fallback: the workflow failed but no deeper signal was recognized. RBAC-style
        # messages are excluded so they never become the reported root cause.
        "category": "GitHub workflow failure",
        "priority": 5,
        "patterns": (
            {"regex": r"##\[error\](?!.*(forbidden|cannot (get|list|watch|create|delete) resource))", "description": "The GitHub workflow step reported an error."},
            {"regex": r"the (operation|job) was canceled", "description": "The GitHub workflow job/step was cancelled."},
            {"regex": r"resource not accessible by integration", "description": "The workflow token lacked permission for a GitHub API operation."},
            {"regex": r"unable to resolve action", "description": "The workflow referenced an action that could not be resolved."},
        ),
        "action": "Open the failing workflow step and review its error output.",
    },
)

# Kubernetes errors are checked FIRST in the failed-job (and pod-log) text; when one is found it is taken as
# the root cause. "symptom_like" entries (CrashLoopBackOff, probe failures ...) are still reported first, but
# any deeper application error found in the same logs is appended to the root cause. Only when none of these
# patterns is present does the regular ROOT_CAUSE_RULES analysis run.
K8S_ERROR_RULES = (
    {
        "category": "Kubernetes: CreateContainerConfigError",
        "priority": 100,
        "patterns": ({"regex": r"CreateContainerConfigError", "description": "The container could not be created because of a configuration error (CreateContainerConfigError) - typically a missing or invalid Secret, ConfigMap or key referenced by the pod."},),
        "action": "Verify the Secrets/ConfigMaps (and the exact keys) referenced by the deployment exist in the target namespace.",
    },
    {
        "category": "Kubernetes: Invalid image name",
        "priority": 99,
        "patterns": ({"regex": r"InvalidImageName", "description": "The pod specifies an invalid container image name (InvalidImageName)."},),
        "action": "Correct the image reference (registry, repository and tag) in the deployment configuration.",
    },
    {
        "category": "Kubernetes: Image pull failure",
        "priority": 98,
        "patterns": (
            {"regex": r"ImagePullBackOff", "description": "The pod could not pull its container image (ImagePullBackOff)."},
            {"regex": r"ErrImagePull", "description": "The pod failed to pull its container image (ErrImagePull)."},
            {"regex": r"ErrImageNeverPull", "description": "The image is not present on the node and pull policy forbids pulling it (ErrImageNeverPull)."},
            {"regex": r"failed to pull image", "description": "Kubernetes failed to pull the container image."},
        ),
        "action": "Verify the image tag exists in the registry, the registry is reachable and the image pull secret is valid.",
    },
    {
        "category": "Kubernetes: Container creation/start error",
        "priority": 97,
        "patterns": (
            {"regex": r"CreateContainerError", "description": "Kubernetes could not create the container (CreateContainerError)."},
            {"regex": r"RunContainerError", "description": "Kubernetes could not start the container (RunContainerError)."},
            {"regex": r"failed to create containerd task", "description": "The container runtime failed to create the container task."},
        ),
        "action": "Inspect the pod events (kubectl describe pod) for the runtime error, and check the command/args, volume mounts and security context.",
    },
    {
        "category": "Kubernetes: OOMKilled",
        "priority": 96,
        "patterns": (
            {"regex": r"OOMKilled", "description": "The container was OOMKilled after exceeding its memory limit."},
            {"regex": r"exit code:?\s*137", "description": "The container exited with code 137 (OOMKilled)."},
        ),
        "action": "Increase the container memory limit/request or tune heap sizing; inspect the pod's previous logs.",
    },
    {
        "category": "Kubernetes: CrashLoopBackOff",
        "priority": 95,
        "symptom_like": True,
        "patterns": (
            {"regex": r"CrashLoopBackOff", "description": "The pod is in CrashLoopBackOff: the container starts and then repeatedly crashes (check the application logs for the exception)."},
            {"regex": r"back-off restarting failed container", "description": "Kubernetes is repeatedly restarting a container that keeps failing (back-off restarting failed container)."},
        ),
        "action": "Inspect the container's previous logs (kubectl logs --previous) to find why the application exits at startup.",
    },
    {
        "category": "Kubernetes: Pod scheduling failure",
        "priority": 94,
        "patterns": (
            {"regex": r"FailedScheduling", "description": "The pod could not be scheduled (FailedScheduling)."},
            {"regex": r"\d+/\d+ nodes are available", "description": "No node satisfies the pod's scheduling requirements."},
            {"regex": r"insufficient (cpu|memory)", "description": "The cluster has insufficient CPU/memory to schedule the pod."},
        ),
        "action": "Check node capacity, resource requests, taints/tolerations and node selectors for the workload.",
    },
    {
        "category": "Kubernetes: Volume mount/attach failure",
        "priority": 93,
        "patterns": (
            {"regex": r"FailedMount", "description": "The pod failed to mount a required volume (FailedMount)."},
            {"regex": r"FailedAttachVolume", "description": "A required persistent volume failed to attach (FailedAttachVolume)."},
            {"regex": r"MountVolume.*failed", "description": "Mounting a required volume failed."},
            {"regex": r"unbound immediate persistentvolumeclaims", "description": "The pod references PersistentVolumeClaims that are not bound."},
        ),
        "action": "Inspect PVC/PV state, storage class and the referenced Secret/ConfigMap volumes.",
    },
    {
        "category": "Kubernetes: Pod evicted",
        "priority": 92,
        "patterns": (
            {"regex": r"the node was low on resource", "description": "The pod was evicted because the node was low on resources."},
            {"regex": r"\bevicted\b.*(pod|node)|(pod|node).*\bevicted\b", "description": "The pod was evicted from its node."},
        ),
        "action": "Check node resource pressure and the pod's resource requests/limits.",
    },
    {
        "category": "Kubernetes: Probe failure",
        "priority": 90,
        "symptom_like": True,
        "patterns": (
            {"regex": r"readiness probe failed", "description": "The pod failed its readiness probe, so it never became ready."},
            {"regex": r"liveness probe failed", "description": "The pod failed its liveness probe and was restarted."},
            {"regex": r"startup probe failed", "description": "The pod failed its startup probe."},
        ),
        "action": "Check the probe path/port/timeouts and the application's startup logs.",
    },
)

# RBAC/authorization-style messages are not treated as deployment root causes in this environment
# and must never classify or influence RCA results. Used only to detect the "RBAC noise only" case.
IGNORED_PATTERNS = (
    r"cannot (get|list|watch|create|delete) resource",
    r"cannot list secrets",
    r"is forbidden",
    r"serviceaccount.*forbidden",
    r"forbidden.*serviceaccount",
    r"(kubectl|oc).*forbidden",
    r"user.*cannot access resource",
)

# Lower-priority symptoms: downstream effects of a deeper cause, never promoted over a root-cause
# signal above. Only used as a last-resort fallback label when no root-cause signal matched at all.
SYMPTOM_RULES = (
    {"category": "Pod crash-loop (symptom only)", "priority": 55, "patterns": (r"CrashLoopBackOff",)},
    {"category": "Readiness/liveness probe failure (symptom only)", "priority": 40, "patterns": (r"readiness probe failed", r"liveness probe failed", r"startup probe failed")},
    {"category": "Deployment timeout (symptom only)", "priority": 30, "patterns": (r"timed out waiting", r"helm upgrade failed", r"pod not ready")},
    {"category": "Progress deadline exceeded (symptom only)", "priority": 20, "patterns": (r"progress deadline exceeded",)},
)

# Run: 0 / NOOP alone is not a failure signal - never used as a root cause.
NOOP_PATTERNS = (r"(?<!Tests )\bRun\s*:\s*0\b", r"deploymentOutcome\s*[:=]\s*NOOP", r"deployment outcome\s*[:=]\s*NOOP")

# ServiceNow Table API and authentication settings.
SNOW_TABLE_API = "change_request"
SNOW_PAGE_SIZE = 200
# Default filters used by the ServiceNow dashboard.
SNOW_DEFAULT_REQUESTED_BY = "fab826d683265e1050ed5c857eda1edd"
SNOW_DEFAULT_ASSIGNMENT_GROUP = "e2c336dfcddd1610afc363290a5261d7"

MAX_WORKERS_ENRICHMENT = 10  # repository topic lookups (one call per repo)
MAX_REPO_WORKERS = 5          # repositories processed in parallel
MAX_LOG_WORKERS = 3           # job-log downloads in parallel inside one repository (<= 15 requests in flight)

# Exponential backoff: retry 1 waits 2s, retry 2 waits 4s ... retry 5 waits 32s (then give up).
RETRY_BACKOFF_SECONDS = (2, 4, 8, 16, 32)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    urllib3.exceptions.ProtocolError,
    http.client.RemoteDisconnected,
)
MAX_RATE_LIMIT_WAIT_SECONDS = 900

FAILED_JOB_CONCLUSIONS = ("failure", "timed_out")
FAILED_DEPLOYMENT_STATES = ("failure", "error")
# Jobs that print the change request number (matched on the job name, e.g.
# "ci-cd / validate / validate-snow-cr / get-cr-number"), in order of preference.
CHG_SOURCE_JOB_NAMES = ("get-cr-number", "check-snow-cr-status")
# "ServiceNow Change Number: CHG0155539" / "Using ServiceNow Change Request Number: CHG0155539"
CHG_LOG_LINE_PATTERN = re.compile(r"Change (?:Request )?Number:\s*(CHG\d{7,})", re.IGNORECASE)
# Job that captures pod/application logs when a deployment fails. It normally *succeeds*, so it is
# found by name (regex, case-insensitive) rather than by conclusion, and its log is added to the RCA.
POD_LOG_JOB_PATTERN = re.compile(r"capture[\s_-]*pod[\s_-]*fail\w*[\s_-]*logs?", re.IGNORECASE)
JOB_ID_PATTERN = re.compile(r"/job/(\d+)")


class RequestFailed(RuntimeError):
    """Raised when a request still fails after all retries (or fails with a non-HTTP error)."""

    def __init__(self, endpoint: str, message: str):
        super().__init__(message)
        self.endpoint = endpoint
        self.message = message


class RetryingClient:
    """requests.Session wrapper: exponential backoff on transient HTTP and network errors."""

    def __init__(self):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.calls = 0
        self._lock = threading.Lock()

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        last_error = ""
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            header_wait = 0
            try:
                resp = self.session.get(url, params=params, timeout=60)
                with self._lock:
                    self.calls += 1
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except requests.exceptions.RequestException as exc:
                raise RequestFailed(url, f"{type(exc).__name__}: {exc}") from exc
            else:
                if resp.status_code == 200:
                    return resp
                rate_limited = resp.status_code == 403 and (
                    resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Remaining") == "0"
                )
                if resp.status_code not in RETRYABLE_STATUS and not rate_limited:
                    return resp  # non-retryable (404, 410, plain 403 ...): caller decides
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    header_wait = int(retry_after)
                elif resp.headers.get("X-RateLimit-Remaining") == "0":
                    header_wait = max(0, int(resp.headers.get("X-RateLimit-Reset", "0")) - int(time.time())) + 1
            if attempt >= len(RETRY_BACKOFF_SECONDS):
                break
            delay = min(max(RETRY_BACKOFF_SECONDS[attempt], header_wait), MAX_RATE_LIMIT_WAIT_SECONDS)
            print(f"[retry] {last_error} on {url}, sleeping {delay}s (retry {attempt + 1}/{len(RETRY_BACKOFF_SECONDS)})", file=sys.stderr)
            time.sleep(delay)
        raise RequestFailed(url, f"gave up after {len(RETRY_BACKOFF_SECONDS)} retries; last error: {last_error}")


class GitHubClient(RetryingClient):
    def __init__(self, token: str):
        super().__init__()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )


class ServiceNowClient(RetryingClient):
    """ServiceNow Table API client using x-sn-apikey."""

    def __init__(self, instance_url: str, api_key: str):
        super().__init__()
        self.instance_url = instance_url.rstrip("/")
        self.session.headers.update({"Content-Type": "application/json", "x-sn-apikey": api_key})

    def table_get(self, table: str, params: dict) -> requests.Response:
        return self.get(f"{self.instance_url}/api/now/table/{table}", params=params)


class CollectionErrors:
    """Thread-safe log of individual API/processing failures; the report is generated regardless."""

    def __init__(self):
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, ctx: dict, endpoint: str, message: str) -> None:
        row = {
            "ChangeNumber": ctx.get("chg") or "N/A",
            "Repository": ctx.get("repository") or "N/A",
            "DeploymentId": str(ctx.get("deployment_id") or "N/A"),
            "WorkflowUrl": ctx.get("workflow_url") or "N/A",
            "ApiEndpoint": endpoint or "N/A",
            "ErrorMessage": message,
        }
        with self._lock:
            self._rows.append(row)
        print(f"[collection-error] {row['ChangeNumber']} {row['Repository']} {row['ApiEndpoint']}: {message}", file=sys.stderr)

    def rows(self) -> list[dict]:
        with self._lock:
            return list(self._rows)


def guarded_get(client: GitHubClient, errors: CollectionErrors, ctx: dict, url: str, params: dict | None = None):
    """GET that never raises: returns the 200 response, or None after recording a collection error."""
    endpoint = url.replace(GITHUB_API, "")
    try:
        resp = client.get(url, params=params)
    except RequestFailed as exc:
        errors.add(ctx, endpoint, exc.message)
        return None
    except Exception as exc:  # noqa: BLE001 - one bad call must not end the report
        errors.add(ctx, endpoint, f"{type(exc).__name__}: {exc}")
        return None
    if resp.status_code != 200:
        errors.add(ctx, endpoint, f"HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    return resp


def environment_aliases(environment: str) -> set[str]:
    env = environment.strip().lower()
    return {"prd", "prod", "production"} if env in ("prd", "prod", "production") else {env}


def _environment_matches(name: str | None, aliases: set[str]) -> bool:
    lowered = (name or "").lower()
    return lowered in aliases or any(t in aliases for t in re.split(r"[-_/ .]+", lowered))


# ---------------------------------------------------------------------------
# ServiceNow: used only to discover repository names of unsuccessful CHGs
# ---------------------------------------------------------------------------
def snow_field(record: dict, field: str) -> str:
    """Raw value of a Table API field (with sysparm_display_value=all each field is {"value", "display_value"})."""
    value = record.get(field)
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip() if value not in (None, "") else ""


def fetch_snow_change_requests(client, start_date, end_date, requested_by, assignment_group) -> list[dict]:
    """Failed (close_code=unsuccessful) change requests of the ServiceNow Deployment Failure
    Dashboard within the window. Fatal on failure: without it there is nothing to analyze."""
    query_parts = [
        f"start_dateBETWEENjavascript:gs.dateGenerate('{start_date}','00:00:00')"
        f"@javascript:gs.dateGenerate('{end_date}','23:59:59')",
        f"requested_by={requested_by}",
        f"assignment_group={assignment_group}",
        "parentISNOTEMPTY",
        "close_code=unsuccessful",
    ]
    fields = (
        "number,short_description,description,u_github_repo,u_github_run_url"
    )
    records: list[dict] = []
    offset = 0
    while True:
        resp = client.table_get(
            SNOW_TABLE_API,
            params={
                "sysparm_query": "^".join(query_parts),
                "sysparm_fields": fields,
                "sysparm_display_value": "all",
                "sysparm_limit": SNOW_PAGE_SIZE,
                "sysparm_offset": offset,
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ServiceNow request failed: HTTP {resp.status_code} {resp.text[:500]}")
        page = resp.json().get("result", []) or []
        records.extend(page)
        if len(page) < SNOW_PAGE_SIZE:
            break
        offset += SNOW_PAGE_SIZE
    return records


def extract_snow_repository(record: dict) -> str:
    """'owner/repo' of a ServiceNow CHG: from the GitHub run URL in its text, else from u_github_repo."""
    text = f"{snow_field(record, 'short_description')}\n{snow_field(record, 'description')}"
    match = GITHUB_RUN_URL_PATTERN.search(text) or GITHUB_RUN_URL_PATTERN.search(snow_field(record, "u_github_run_url"))
    return match.group("repository") if match else snow_field(record, "u_github_repo")


def _parse_github_timestamp(value: str | None):
    """Parse a GitHub API UTC timestamp (e.g. 2026-09-29T12:34:56Z)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def group_jobs_by_run_attempt(jobs: list[dict]) -> dict[str, list[dict]]:
    """Group workflow jobs (fetched with filter=all) by their run_attempt number."""
    groups: dict[str, list[dict]] = {}
    for job in jobs:
        attempt = str(job.get("run_attempt") or "1")
        groups.setdefault(attempt, []).append(job)
    return groups


# Leading timestamp of a raw job-log line (optionally preceded by a BOM) and ANSI colour codes.
LOG_TIMESTAMP_PREFIX = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _log_marker(line: str) -> str:
    """Line without ANSI codes and timestamp, lower-cased (used to recognise ##[group] markers)."""
    return LOG_TIMESTAMP_PREFIX.sub("", ANSI_ESCAPE_PATTERN.sub("", line)).strip().lower()


def _step_definition_end(lines: list[str], start: int) -> int | None:
    """Index of the closing ##[endgroup] if lines[start] opens the definition of a `run:` step, else None.

    A `run:` step definition always contains a `shell: ...` line; groups a script prints itself
    (echo "::group::Run tests") do not, so they are never mistaken for a step definition."""
    has_shell = False
    for index in range(start + 1, len(lines)):
        marker = _log_marker(lines[index])
        if marker.startswith("##[endgroup]"):
            return index if has_shell else None
        if marker.startswith("shell:"):
            has_shell = True
    return None


def strip_step_definitions(log_text: str) -> str:
    """Remove the echoed step definitions (the full `run:` script, shell and env block) from a job log.

    GitHub prints every `run:` step as a collapsed `##[group]Run ...` ... `##[endgroup]` block before the
    step's real output. That text is workflow source code, not runtime output, so keywords inside it (a
    script that greps for CrashLoopBackOff, hint/root_cause strings ...) must never count as evidence.
    A group that is never closed is kept untouched."""
    lines = log_text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if _log_marker(lines[index]).startswith("##[group]run "):
            end = _step_definition_end(lines, index)
            if end is not None:
                index = end + 1
                continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept)


def _is_non_evidence_line(line: str) -> bool:
    """Reject shell/diagnostic code and API debug payloads - only runtime output is evidence."""
    normalized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).lower()
    # Raw log lines start with a timestamp; line-anchored checks must look at the text after it.
    content = re.sub(r"^\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}\.\d+z\s+", "", normalized)
    if content.lstrip().startswith(("##[group]", "##[endgroup]")):
        return True
    if re.search(r"step\s+\d+\s+response", normalized):
        return True
    if '"result"' in normalized and ('"parent"' in normalized or '"sys_id"' in normalized or "service-now.com" in normalized):
        return True
    if any(token in normalized for token in ("pod_signal_blob", "workload_file", "reasons=", "reasons)")):
        return True
    if any(cmd in normalized for cmd in ("grep ", "egrep ", "awk ", "sed -n", "sed '")):
        return True
    if normalized.count("|") >= 3:
        return True
    if re.match(r"^\s*(if|elif|while|case)\b.*\b(then|do)\b", content):
        return True
    if re.search(r"\bif echo\b", normalized) or re.search(r'case\s+"\$', normalized):
        return True
    if content.lstrip().startswith(("#!/", "function ", "set -e", "set -euo pipefail", "run #")):
        return True
    # Shell/script comment echoed into the log.
    if content.startswith("#") and not content.startswith("##"):
        return True
    # Self-declared non-fatal diagnostics and GH Actions warnings are not failures.
    if "::warning" in normalized or "##[warning" in normalized:
        return True
    if "without failing deployment" in normalized or "continuing without failing" in normalized:
        return True
    # Generic GH Actions wrapper message carries no root-cause information.
    if "process completed with exit code" in normalized:
        return True
    # Git log/branch-ref noise (e.g. branch names that happen to contain failure keywords).
    if "[new branch]" in normalized or "-> origin/" in normalized:
        return True
    return False


def _dedupe_repeated_lines(lines: list[str], max_repeats: int = 2) -> list[str]:
    """Collapse long runs of near-identical polling/noise lines, keeping only the first few."""
    deduped: list[str] = []
    previous_key = None
    run_count = 0
    for line in lines:
        key = re.sub(r"\d+", "#", line.strip().lower())
        if key and key == previous_key:
            run_count += 1
            if run_count <= max_repeats:
                deduped.append(line)
        else:
            previous_key = key
            run_count = 1
            deduped.append(line)
    return deduped


def _evidence_snippet(log_text: str, pattern: str) -> str:
    """Return the first matching runtime line only (used for a quick existence check)."""
    matcher = re.compile(pattern, re.IGNORECASE)
    for line in log_text.splitlines():
        if _is_non_evidence_line(line):
            continue
        if matcher.search(line):
            clean_line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
            return re.sub(r"\s+", " ", clean_line).strip()
    return ""


def _evidence_context(log_text: str, pattern: str) -> tuple[str, str]:
    """Return (context window of 5 before / match / 10 after, the matching line) for the first match."""
    matcher = re.compile(pattern, re.IGNORECASE)
    raw_lines = log_text.splitlines()
    deduped_lines = _dedupe_repeated_lines(raw_lines)
    for idx, line in enumerate(deduped_lines):
        if _is_non_evidence_line(line):
            continue
        if not matcher.search(line):
            continue
        start = max(0, idx - CONTEXT_LINES_BEFORE)
        end = min(len(deduped_lines), idx + CONTEXT_LINES_AFTER + 1)
        window = [
            re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", w)).strip()
            for w in deduped_lines[start:end]
            if not _is_non_evidence_line(w)
        ]
        text = "\n".join(w for w in window if w)
        matched_line = re.sub(r"\s+", " ", LOG_TIMESTAMP_PREFIX.sub("", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line))).strip()
        return text[:MAX_LOG_EVIDENCE_LENGTH], matched_line
    return "", ""


def extract_change_numbers(texts) -> list[str]:
    """Distinct CHG numbers found in the given texts, in order of first appearance."""
    found: list[str] = []
    for text in texts:
        for match in CHANGE_NUMBER_PATTERN.finditer(text or ""):
            number = match.group(0).upper()
            if number not in found:
                found.append(number)
    return found


def summarize_error(analysis: dict) -> str:
    evidence = analysis.get("evidence") or ""
    lines = [l.strip() for l in evidence.splitlines() if l.strip()]
    for line in lines:
        if re.search(r"error|fail|exception|denied|not found|timeout|timed out|unauthorized|ORA-|refused", line, re.IGNORECASE):
            return line[:200]
    return (lines[0] if lines else analysis.get("root_cause") or "N/A")[:200]


# ---------------------------------------------------------------------------
# GitHub: repository -> PRD deployments -> failed deployment events
# ---------------------------------------------------------------------------
def fetch_repo_deployments(client, errors, ctx, repo, environment, start_dt, end_dt) -> tuple[list[dict], bool]:
    """PRD deployments of one repo within [start_dt, end_dt], newest first. Returns (deployments, ok).

    Tries GitHub's `environment` filter first; if that yields nothing (naming may differ from
    ServiceNow), retries unfiltered and keeps only deployments whose environment name matches
    the requested environment or its aliases (prd/prod/production)."""
    aliases = environment_aliases(environment)
    ok = True
    for use_filter in (True, False):
        ok = True
        deployments: list[dict] = []
        page = 1
        while True:
            params = {"per_page": 100, "page": page}
            if use_filter:
                params["environment"] = environment
            resp = guarded_get(client, errors, ctx, f"{GITHUB_API}/repos/{repo}/deployments", params)
            if resp is None:
                ok = False
                break
            batch = resp.json() or []
            if not batch:
                break
            reached_older = False
            for deployment in batch:
                created_at = _parse_github_timestamp(deployment.get("created_at"))
                if created_at is None:
                    continue
                if created_at < start_dt:
                    reached_older = True
                    continue
                if created_at > end_dt:
                    continue
                if not use_filter and not _environment_matches(deployment.get("environment"), aliases):
                    continue
                deployments.append(deployment)
            if reached_older or len(batch) < 100:
                break
            page += 1
        if deployments:
            return deployments, ok
    return [], ok


def fetch_failed_events(client, errors, ctx, repo, deployment) -> tuple[list[dict], bool]:
    """Status history of one deployment -> failed events (one per run attempt)."""
    resp = guarded_get(
        client, errors, {**ctx, "deployment_id": deployment["id"]},
        f"{GITHUB_API}/repos/{repo}/deployments/{deployment['id']}/statuses", {"per_page": 100},
    )
    if resp is None:
        return [], False
    events: dict[tuple, dict] = {}
    for status in resp.json() or []:
        if status.get("state") not in FAILED_DEPLOYMENT_STATES:
            continue
        url = status.get("log_url") or status.get("target_url") or ""
        run = GITHUB_RUN_URL_PATTERN.search(url)
        job = JOB_ID_PATTERN.search(url)
        created_at = _parse_github_timestamp(status.get("created_at")) or _parse_github_timestamp(deployment.get("created_at"))
        if created_at is None:
            continue
        event = {
            "deployment_id": deployment["id"],
            "status_id": status.get("id"),
            "created_at": created_at,
            "run_id": run.group("run_id") if run else "",
            "attempt": (run.group("attempt") or "") if run else "",
            "job_id": job.group(1) if job else "",
        }
        key = (event["deployment_id"], event["run_id"], event["attempt"])
        if key not in events or events[key]["created_at"] < created_at:
            events[key] = event
    return list(events.values()), True


# ---------------------------------------------------------------------------
# GitHub: failed deployment -> workflow run -> failed jobs -> failed logs only
# ---------------------------------------------------------------------------
def get_run(client, errors, ctx, repo, run_id, cache) -> dict | None:
    if run_id not in cache["runs"]:
        resp = guarded_get(client, errors, ctx, f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}")
        cache["runs"][run_id] = resp.json() if resp is not None else None
    return cache["runs"][run_id]


def get_jobs(client, errors, ctx, repo, run_id, attempt, cache) -> list[dict] | None:
    """Jobs of one attempt when known (cheapest), otherwise every attempt of the run."""
    key = (run_id, attempt or "all")
    if key in cache["jobs"]:
        return cache["jobs"][key]
    if attempt:
        url, params = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", {"per_page": 100}
    else:
        url, params = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100, "filter": "all"}
    jobs: list[dict] | None = []
    page = 1
    while True:
        resp = guarded_get(client, errors, ctx, url, {**params, "page": page})
        if resp is None:
            jobs = None
            break
        batch = resp.json().get("jobs", []) or []
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    cache["jobs"][key] = jobs
    return jobs


def choose_failed_attempt(jobs: list[dict], event: dict) -> str | None:
    """Which rerun attempt actually failed for this deployment event."""
    if event.get("job_id"):
        for job in jobs:
            if str(job.get("id")) == event["job_id"]:
                return str(job.get("run_attempt") or "1")
    failed = {
        attempt: [j for j in attempt_jobs if j.get("conclusion") in FAILED_JOB_CONCLUSIONS]
        for attempt, attempt_jobs in group_jobs_by_run_attempt(jobs).items()
    }
    failed = {attempt: js for attempt, js in failed.items() if js}
    if not failed:
        return None

    def distance(attempt: str) -> float:
        times = [_parse_github_timestamp(j.get("completed_at")) for j in failed[attempt]]
        times = [t for t in times if t]
        return abs((max(times) - event["created_at"]).total_seconds()) if times else float("inf")

    return min(failed, key=distance)


def find_change_number_from_cr_jobs(client, errors, ctx, repo, run_id, attempt, attempt_jobs, cache) -> tuple[str, str]:
    """Read the CHG from the log of the get-cr-number / check-snow-cr-status job of the failed attempt
    (one log download, stopping at the first hit). Returns (chg, source description) or ("", "")."""
    def candidates(jobs: list[dict]) -> list[dict]:
        found = []
        for name in CHG_SOURCE_JOB_NAMES:
            found += [j for j in jobs if name in (j.get("name") or "").lower() and j.get("id") is not None]
        return found

    cr_jobs = candidates(attempt_jobs)
    if not cr_jobs:  # a rerun attempt may not list jobs carried over from earlier attempts
        all_jobs = get_jobs(client, errors, ctx, repo, run_id, None, cache) or []
        limit = int(attempt) if str(attempt).isdigit() else None
        earlier = [j for j in all_jobs if limit is None or int(j.get("run_attempt") or 1) <= limit]
        # newest attempt first, so the closest earlier attempt wins
        cr_jobs = sorted(candidates(earlier), key=lambda j: -int(j.get("run_attempt") or 1))
    for job in cr_jobs:
        resp = guarded_get(client, errors, ctx, f"{GITHUB_API}/repos/{repo}/actions/jobs/{job['id']}/logs")
        if resp is None:
            continue
        log_text = strip_step_definitions(resp.text)
        match = CHG_LOG_LINE_PATTERN.search(log_text) or CHANGE_NUMBER_PATTERN.search(log_text)
        if match:
            number = (match.group(1) if match.lastindex else match.group(0)).upper()
            return number, f"job '{job.get('name')}'"
    return "", ""


def _new_record(repo: str, environment: str) -> dict:
    return {
        "ChangeNumber": "N/A",
        "Repository": repo or "N/A",
        "Environment": environment,
        "DeploymentId": "N/A",
        "WorkflowRunUrl": "N/A",
        "DeploymentTime": "N/A",
        "DeploymentDate": "N/A",
        "DeploymentStatus": "N/A",
        "FailedAttempt": "N/A",
        "FailedJobs": [],
        "FailedSteps": [],
        "WorkflowChangeNumbers": [],
        "ChgSource": "N/A",
        "PodLogJob": "N/A",
        "Owner": "N/A",
        "Technology": "N/A",
        "FailureCategory": "N/A",
        "RootCause": "N/A",
        "ErrorSummary": "N/A",
        "Evidence": "N/A",
        "SourceJob": "N/A",
        "RecommendedAction": "N/A",
        "DataQualityNotes": [],
        "RootCauseAnalysis": {},
    }


def _apply_analysis(record: dict, analysis: dict) -> None:
    record["RootCauseAnalysis"] = analysis
    record["FailureCategory"] = analysis["failure_category"]
    record["RootCause"] = analysis["root_cause"]
    record["Evidence"] = analysis.get("evidence") or "N/A"
    record["ErrorSummary"] = (analysis.get("error_line") or summarize_error(analysis))[:200]
    record["SourceJob"] = analysis.get("source_job") or "N/A"
    record["RecommendedAction"] = analysis.get("recommended_action") or "N/A"


def _simple_analysis(category: str, root_cause: str, action: str, evidence: str = "") -> dict:
    return {"failure_category": category, "root_cause": root_cause, "evidence": evidence or root_cause,
            "recommended_action": action, "source_job": ""}


# Generic findings that do not explain *why* a deployment failed. When the failed jobs only yield these
# (or nothing), the pod/application log job is consulted as well.
GENERIC_CATEGORIES = {"GitHub workflow failure", "Application startup failure", "Helm failure", "Deployment timeout"}


def _rca_strength(analysis: dict | None) -> int:
    """0 = nothing found, 1 = generic/symptom only (inconclusive), 2 = a real root cause."""
    if analysis is None or analysis["failure_category"] == "Insufficient deployment evidence":
        return 0
    if analysis["failure_category"] in GENERIC_CATEGORIES or analysis["failure_category"].endswith("(symptom only)"):
        return 1
    return 2


def collection_failure_record(repo: str, environment: str, reason: str, event: dict | None = None) -> dict:
    record = _new_record(repo, environment)
    if event:
        record["DeploymentId"] = str(event["deployment_id"])
        record["DeploymentTime"] = event["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
        record["DeploymentDate"] = event["created_at"].strftime("%Y-%m-%d")
        record["DeploymentStatus"] = "Failure"
        if event["run_id"]:
            record["WorkflowRunUrl"] = f"https://github.com/{repo}/actions/runs/{event['run_id']}"
    _apply_analysis(record, _simple_analysis(
        "Collection failure", f"Root cause analysis unavailable: {reason}",
        "Open the workflow run attempt and inspect the failed job logs manually.", reason))
    return record


def analyze_failed_deployment(client, errors, cache, repo: str, event: dict, environment: str) -> dict:
    ctx = {"chg": "N/A", "repository": repo, "deployment_id": str(event["deployment_id"]), "workflow_url": ""}
    record = _new_record(repo, environment)
    record["DeploymentId"] = str(event["deployment_id"])
    record["DeploymentTime"] = event["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
    record["DeploymentDate"] = event["created_at"].strftime("%Y-%m-%d")
    record["DeploymentStatus"] = "Failure"
    notes = record["DataQualityNotes"]

    run_id = event["run_id"]
    if not run_id:
        notes.append("The failed deployment status did not reference a GitHub workflow run.")
        _apply_analysis(record, _simple_analysis(
            "Insufficient deployment evidence", "The failed deployment has no associated workflow run URL.",
            "Inspect the deployment's status history in GitHub."))
        return record

    # Workflow run and the jobs of the failed attempt.
    run = get_run(client, errors, ctx, repo, run_id, cache)
    base_url = (run or {}).get("html_url") or f"https://github.com/{repo}/actions/runs/{run_id}"
    ctx["workflow_url"] = base_url
    record["WorkflowRunUrl"] = base_url

    jobs = get_jobs(client, errors, ctx, repo, run_id, event["attempt"], cache)
    if jobs is None:
        record["WorkflowRunUrl"] = f"{base_url}/attempts/{event['attempt']}" if event["attempt"] else base_url
        record["FailedAttempt"] = event["attempt"] or "N/A"
        _apply_analysis(record, _simple_analysis(
            "Collection failure", "Root cause analysis unavailable: workflow jobs could not be retrieved.",
            "Open the workflow run and inspect the failed job logs manually."))
        return record

    attempt = event["attempt"] or choose_failed_attempt(jobs, event) or str((run or {}).get("run_attempt") or "")
    record["FailedAttempt"] = attempt or "N/A"
    record["WorkflowRunUrl"] = f"{base_url}/attempts/{attempt}" if attempt else base_url

    attempt_jobs = [j for j in jobs if not attempt or str(j.get("run_attempt") or "1") == str(attempt)]
    failed_jobs = [j for j in attempt_jobs if j.get("conclusion") in FAILED_JOB_CONCLUSIONS and j.get("id") is not None]
    if not failed_jobs:  # e.g. the job was cancelled by a timeout; never skipped/successful jobs
        failed_jobs = [j for j in attempt_jobs if j.get("conclusion") == "cancelled" and j.get("id") is not None]
    record["FailedJobs"] = [j.get("name", str(j.get("id"))) for j in failed_jobs]
    record["FailedSteps"] = [
        f"{j.get('name')} > {s.get('name')}"
        for j in failed_jobs for s in (j.get("steps") or []) if s.get("conclusion") == "failure"
    ]
    if not failed_jobs:
        notes.append("No failed jobs were recorded for the failed attempt.")
        _apply_analysis(record, _simple_analysis(
            "Insufficient deployment evidence", "No failed jobs were found for the failed deployment attempt.",
            "Open the workflow run and inspect it manually."))
        return record

    # Logs of the failed jobs only (echoed step definitions / workflow script source are removed).
    log_by_job: dict[str, str] = {}
    log_failures = 0
    with ThreadPoolExecutor(max_workers=MAX_LOG_WORKERS) as executor:
        futures = {
            executor.submit(guarded_get, client, errors, ctx, f"{GITHUB_API}/repos/{repo}/actions/jobs/{j['id']}/logs"): j
            for j in failed_jobs
        }
        for future in as_completed(futures):
            job = futures[future]
            resp = future.result()
            log_by_job[str(job["id"])] = strip_step_definitions(resp.text) if resp is not None else ""
            log_failures += resp is None

    # CHG: log of the get-cr-number / check-snow-cr-status job; fallbacks are the failed jobs' logs, then
    # run metadata (single-attempt runs only, since metadata is shared by every rerun attempt).
    run = run or {}
    cr_chg, cr_source = find_change_number_from_cr_jobs(client, errors, ctx, repo, run_id, attempt, attempt_jobs, cache)
    log_chgs = extract_change_numbers(log_by_job.values())
    meta_chgs = []
    if int(run.get("run_attempt") or 1) == 1:
        meta_chgs = extract_change_numbers([run.get("display_title"), run.get("name"), (run.get("head_commit") or {}).get("message")])
    if cr_chg:
        workflow_chgs, record["ChgSource"] = [cr_chg], cr_source
    elif log_chgs:
        workflow_chgs, record["ChgSource"] = log_chgs, "failed job logs"
    else:
        workflow_chgs, record["ChgSource"] = meta_chgs, ("run metadata" if meta_chgs else "N/A")
    record["WorkflowChangeNumbers"] = workflow_chgs
    if workflow_chgs:
        record["ChangeNumber"] = ctx["chg"] = workflow_chgs[0]
    else:
        notes.append("No CHG number was found in the get-cr-number/check-snow-cr-status job, the failed job logs or the run metadata.")

    # RCA pass 1: failed jobs only (Kubernetes errors first, then the regular rules).
    analysis = analyze_jobs(failed_jobs, log_by_job) if log_failures < len(failed_jobs) else None
    if analysis is not None and log_failures:
        analysis["root_cause"] += f" (Partial evidence: {log_failures} failed job log(s) could not be retrieved.)"

    # RCA pass 2, only when pass 1 is empty or inconclusive: add the pod/application log job.
    if _rca_strength(analysis) < 2:
        pod_job = next(
            (j for j in attempt_jobs if POD_LOG_JOB_PATTERN.search(j.get("name") or "") and j.get("id") is not None
             and j["id"] not in {f["id"] for f in failed_jobs}),
            None,
        )
        if pod_job is None:
            notes.append("No 'capture pod failure logs' job was found for this attempt.")
        else:
            pod_resp = guarded_get(client, errors, ctx, f"{GITHUB_API}/repos/{repo}/actions/jobs/{pod_job['id']}/logs")
            if pod_resp is not None:
                log_by_job[str(pod_job["id"])] = strip_step_definitions(pod_resp.text)
                with_pod = analyze_jobs(failed_jobs + [pod_job], log_by_job)
                if _rca_strength(with_pod) > _rca_strength(analysis):
                    analysis = with_pod
                    record["PodLogJob"] = pod_job.get("name") or str(pod_job["id"])

    if analysis is None:
        analysis = _simple_analysis(
            "Log retrieval failure", "Root cause analysis unavailable: no failed job log could be retrieved.",
            "Open the workflow run attempt and inspect the failed job logs manually (logs may have expired).")
    _apply_analysis(record, analysis)
    return record


def process_repository(client, errors, repo, environment, range_start, range_end) -> list[dict]:
    """GitHub is the source of rows: every failed PRD deployment of the repo within the date range is
    analyzed (workflow run -> failed jobs -> failed logs -> CHG + RCA)."""
    status_start, status_end = range_start, range_end + timedelta(days=1)  # end date inclusive
    ctx = {"chg": "N/A", "repository": repo}
    deployments, lookup_ok = fetch_repo_deployments(
        client, errors, ctx, repo, environment, status_start - timedelta(days=1), status_end + timedelta(days=1)
    )
    events: list[dict] = []
    for deployment in deployments:
        found, ok = fetch_failed_events(client, errors, ctx, repo, deployment)
        events.extend(e for e in found if status_start <= e["created_at"] < status_end)
        lookup_ok = lookup_ok and ok

    cache = {"runs": {}, "jobs": {}}
    records: list[dict] = []
    for event in sorted(events, key=lambda e: e["created_at"]):
        try:
            records.append(analyze_failed_deployment(client, errors, cache, repo, event, environment))
        except Exception as exc:  # noqa: BLE001 - never abort the report
            errors.add({**ctx, "deployment_id": event["deployment_id"]}, "analysis", f"{type(exc).__name__}: {exc}")
            records.append(collection_failure_record(repo, environment, f"{type(exc).__name__}: {exc}", event))
    if not lookup_ok and not events:
        records.append(collection_failure_record(
            repo, environment, "GitHub deployment data could not be retrieved for this repository; see Collection Errors."))
    return records


def fetch_repo_topics(client: GitHubClient, errors: CollectionErrors, repo_full_name: str) -> tuple[str, str]:
    """Return owner and technology topics (never raises)."""
    resp = guarded_get(client, errors, {"repository": repo_full_name}, f"{GITHUB_API}/repos/{repo_full_name}/topics")
    if resp is None:
        return "N/A", "N/A"
    owner = technology = "N/A"
    for topic in resp.json().get("names", []) or []:
        if owner == "N/A" and topic.startswith("owner-"):
            owner = topic[len("owner-"):] or "N/A"
        elif technology == "N/A" and topic.startswith("technology-"):
            technology = topic[len("technology-"):] or "N/A"
    return owner, technology


def enrich_records_with_topics(records: list[dict], client: GitHubClient, errors: CollectionErrors) -> None:
    unique_repos = sorted({r["Repository"] for r in records if r["Repository"] not in ("N/A", "")})
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_ENRICHMENT) as executor:
        futures = {executor.submit(fetch_repo_topics, client, errors, repo): repo for repo in unique_repos}
        topics_by_repo: dict[str, tuple[str, str]] = {}
        for future in as_completed(futures):
            try:
                topics_by_repo[futures[future]] = future.result()
            except Exception as exc:  # noqa: BLE001
                errors.add({"repository": futures[future]}, "/topics", f"{type(exc).__name__}: {exc}")
    for record in records:
        record["Owner"], record["Technology"] = topics_by_repo.get(record["Repository"], ("N/A", "N/A"))


def rank_by_field(records: list[dict], field: str, top_n: int = 10) -> list[tuple[str, int, float]]:
    total = len(records)
    return [(v, c, (c / total * 100) if total else 0.0) for v, c in Counter(r[field] for r in records).most_common(top_n)]


def _collect_rule_signals(rules, jobs: list[dict], log_by_job: dict[str, str]) -> list[dict]:
    """For each rule, the best (richest-evidence) match across the given jobs' logs."""
    signals = []
    for rule in rules:
        best = None
        for job in jobs:
            log_text = log_by_job.get(str(job.get("id")), "")
            for pattern in rule["patterns"]:
                context, matched_line = _evidence_context(log_text, pattern["regex"])
                if not context:
                    continue
                description = pattern["description"](context) if callable(pattern["description"]) else pattern["description"]
                candidate = {
                    "category": rule["category"],
                    "priority": rule["priority"],
                    "evidence": context,
                    "error_line": matched_line,
                    "source_job": job.get("name", "unknown job"),
                    "description": description,
                    "action": rule.get("action", ""),
                    "symptom_like": rule.get("symptom_like", False),
                }
                if best is None or len(candidate["evidence"]) > len(best["evidence"]):
                    best = candidate
        if best:
            signals.append(best)
    return signals


# ---------------------------------------------------------------------------
# "INTELLIGENT FAILURE SUMMARY" block printed by the Deploy helm chart job (Dynamic Replica and Pod
# Readiness Check). Two layouts are supported:
#   ===== INTELLIGENT FAILURE SUMMARY =====      === INTELLIGENT FAILURE SUMMARY ===
#   ROOT_CAUSE=... EXACT_ERROR_LINE=...           Context: ... / Root Cause: ... / Exact Signal: ...
#   SUGGESTED_ACTION=... TOP_SIGNALS=...          Suggested Action: ...
# ---------------------------------------------------------------------------
FAILURE_SUMMARY_START = re.compile(r"^=+\s*intelligent failure summary\s*=+$", re.IGNORECASE)
FAILURE_SUMMARY_FIELD = re.compile(
    r"^(root[ _]cause|exact[ _]error[ _]line|exact[ _]signal|suggested[ _]action|diagnostic[ _]limitation"
    r"|diagnostics[ _]dir|context|top[ _]signals)\s*[=:]\s*(.*)$",
    re.IGNORECASE,
)
FAILURE_SUMMARY_MAX_LINES = 60
INCONCLUSIVE_ROOT_CAUSE = re.compile(
    r"\b(unknown|undetermined|inconclusive|unable to determine)\b|no (clear|specific|obvious) root cause", re.IGNORECASE
)


def _clean_log_line(line: str) -> str:
    return LOG_TIMESTAMP_PREFIX.sub("", ANSI_ESCAPE_PATTERN.sub("", line)).strip()


def _parse_failure_summaries(log_text: str) -> list[dict]:
    """Every failure-summary block in a log, as {root_cause, error_line, action, lines}."""
    lines = log_text.splitlines()
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        if not FAILURE_SUMMARY_START.match(_clean_log_line(lines[index])):
            index += 1
            continue
        fields: dict[str, str] = {}
        kept: list[str] = []
        current = None
        end = index + 1
        while end < len(lines) and end - index <= FAILURE_SUMMARY_MAX_LINES:
            text = _clean_log_line(lines[end])
            if re.fullmatch(r"=+", text) or text.startswith("##[") or FAILURE_SUMMARY_START.match(text):
                break
            field = FAILURE_SUMMARY_FIELD.match(text)
            if field:
                current = re.sub(r"[ _]+", "_", field.group(1).lower())
                fields.setdefault(current, field.group(2).strip())
                kept.append(text)
                if current == "diagnostics_dir":
                    end += 1
                    break
            elif current == "top_signals" and text:
                kept.append(text)
            end += 1
        index = max(end, index + 1)
        error_line = re.sub(r"\s+", " ", fields.get("exact_error_line") or fields.get("exact_signal") or "").strip()
        if fields.get("root_cause"):
            blocks.append({
                "root_cause": fields["root_cause"].strip(),
                "error_line": error_line,
                "action": fields.get("suggested_action", "").strip(),
                # RBAC "forbidden" lines are diagnostic limitations, never part of the reported evidence.
                "lines": [l for l in kept if not any(re.search(p, l, re.IGNORECASE) for p in IGNORED_PATTERNS)],
            })
    return blocks


def _match_k8s_rule(text: str) -> dict | None:
    """Highest-priority Kubernetes rule whose pattern appears in the text."""
    best = None
    for rule in K8S_ERROR_RULES:
        if any(re.search(p["regex"], text, re.IGNORECASE) for p in rule["patterns"]):
            if best is None or rule["priority"] > best["priority"]:
                best = rule
    return best


def _signal_result(signal: dict) -> dict:
    return {
        "failure_category": signal["category"],
        "root_cause": signal["description"],
        "evidence": signal["evidence"],
        "error_line": signal["error_line"],
        "recommended_action": signal["action"],
        "source_job": signal["source_job"],
    }


def _failure_summary_analysis(jobs: list[dict], log_by_job: dict[str, str]) -> dict | None:
    """RCA taken from the workflow's own failure summary (the last conclusive block wins). The category comes
    from the Kubernetes rules applied to the block's root cause / exact signal, when one matches."""
    chosen = None
    for job in jobs:
        for block in _parse_failure_summaries(log_by_job.get(str(job.get("id")), "")):
            if not INCONCLUSIVE_ROOT_CAUSE.search(block["root_cause"]):
                chosen = (job, block)
    if chosen is None:
        return None
    job, block = chosen
    rule = _match_k8s_rule(f"{block['error_line']} {block['root_cause']}")
    result = {
        "failure_category": rule["category"] if rule else "Deployment failure (workflow summary)",
        "root_cause": block["root_cause"],
        "evidence": "\n".join(block["lines"])[:MAX_LOG_EVIDENCE_LENGTH],
        "error_line": block["error_line"] or block["root_cause"],
        "recommended_action": block["action"] or (rule["action"] if rule else "Review the failure summary in the deployment job log."),
        "source_job": job.get("name", "unknown job"),
    }
    if rule and rule.get("symptom_like"):  # e.g. CrashLoopBackOff: keep a deeper application error visible
        standard = _analyze_standard(jobs, log_by_job)
        if _rca_strength(standard) == 2:
            result["root_cause"] += f" Underlying error seen in the logs: {standard['root_cause']}"
    return result


def analyze_jobs(jobs: list[dict], log_by_job: dict[str, str]) -> dict:
    """RCA order for the given jobs:
      1. missing CI/CD variables/secrets (placeholder replacement failed)
      2. the workflow's "INTELLIGENT FAILURE SUMMARY" block (categorised with the Kubernetes rules)
      3. Kubernetes errors (CreateContainerConfigError, ImagePullBackOff, OOMKilled, CrashLoopBackOff ...)
      4. the regular ROOT_CAUSE_RULES
    """
    if jobs:
        missing = _collect_rule_signals((MISSING_VARIABLES_RULE,), jobs, log_by_job)
        if missing:
            return _signal_result(missing[0])

        summary = _failure_summary_analysis(jobs, log_by_job)
        if summary:
            return summary

        k8s_signals = _collect_rule_signals(K8S_ERROR_RULES, jobs, log_by_job)
        if k8s_signals:
            k8s_signals.sort(key=lambda s: (s["priority"], len(s["evidence"])), reverse=True)
            primary = k8s_signals[0]
            result = _signal_result(primary)
            if primary["symptom_like"]:  # keep a deeper application error visible, if the logs show one
                standard = _analyze_standard(jobs, log_by_job)
                if _rca_strength(standard) == 2:
                    result["root_cause"] += f" Underlying error seen in the logs: {standard['root_cause']}"
            return result
    return _analyze_standard(jobs, log_by_job)


def _analyze_standard(jobs: list[dict], log_by_job: dict[str, str]) -> dict:
    """Regular (non-Kubernetes-first) analysis: select the strongest root cause across the given jobs.

    Root-cause rules always outrank symptom rules (e.g. CrashLoopBackOff, probe failures,
    deployment timeouts) regardless of how many jobs mention them, since symptoms are downstream
    effects of a deeper cause rather than causes themselves. RBAC/authorization messages are never
    treated as a root cause in this environment (see IGNORED_PATTERNS).
    """
    insufficient_evidence_message = "No actionable deployment failure could be determined from the available logs."
    empty_result = {
        "failure_category": "Insufficient deployment evidence",
        "root_cause": insufficient_evidence_message,
        "evidence": "",
        "recommended_action": "Open the workflow run and inspect the failed job logs.",
        "source_job": "",
    }
    if not jobs:
        return empty_result

    def _collect_symptom_signals() -> list[dict]:
        signals = []
        for rule in SYMPTOM_RULES:
            for job in jobs:
                log_text = log_by_job.get(str(job.get("id")), "")
                for pattern in rule["patterns"]:
                    snippet = _evidence_snippet(log_text, pattern)
                    if snippet:
                        signals.append({"category": rule["category"], "priority": rule["priority"], "evidence": snippet, "source_job": job.get("name", "unknown job")})
        return signals

    root_signals = _collect_rule_signals(ROOT_CAUSE_RULES, jobs, log_by_job)
    symptom_signals = _collect_symptom_signals()

    if not root_signals:
        rbac_noise_found = any(
            _evidence_snippet(log_by_job.get(str(job.get("id")), ""), pattern)
            for job in jobs
            for pattern in IGNORED_PATTERNS
        )
        is_noop = any(
            _evidence_snippet(log_by_job.get(str(job.get("id")), ""), pattern)
            for job in jobs
            for pattern in NOOP_PATTERNS
        )
        if rbac_noise_found and not symptom_signals and not is_noop:
            return {
                **empty_result,
                "root_cause": "No actionable deployment failure was identified from the available runtime logs.",
            }
        if symptom_signals:
            symptom_signals.sort(key=lambda s: s["priority"], reverse=True)
            top_symptom = symptom_signals[0]
            summary = (
                f"Only the symptom '{top_symptom['category']}' was observed; the underlying "
                "technical cause was not found in the available logs."
            )
            return {
                "failure_category": top_symptom["category"],
                "root_cause": summary,
                "evidence": top_symptom["evidence"],
                "error_line": top_symptom["evidence"],
                "recommended_action": "Inspect the pod/workload events and application logs preceding this symptom to find the underlying cause.",
                "source_job": top_symptom["source_job"],
            }
        if is_noop:
            summary = (
                "Deployment reported Run: 0 / NOOP with no other failure evidence found; "
                "this is likely not a true application failure."
            )
            return {**empty_result, "root_cause": summary, "evidence": summary}
        return empty_result

    # Root-cause signals always win over symptoms; among root causes, higher priority then
    # richer evidence wins.
    root_signals.sort(key=lambda s: (s["priority"], len(s["evidence"])), reverse=True)
    primary = root_signals[0]

    return {
        "failure_category": primary["category"],
        "root_cause": primary["description"],
        "evidence": primary["evidence"],
        "error_line": primary["error_line"],
        "recommended_action": primary["action"] or "Inspect the strongest evidence in the failed job and dependent workload diagnostics before retrying.",
        "source_job": primary["source_job"],
    }


def _md(value, limit: int | None = None) -> str:
    text = str(value if value not in (None, "") else "N/A").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text[:limit] + "..." if limit and len(text) > limit else text


def _error_count(record: dict, error_rows: list[dict]) -> int:
    if record["DeploymentId"] == "N/A":
        return 0
    return sum(1 for e in error_rows if e["Repository"] == record["Repository"] and e["DeploymentId"] == record["DeploymentId"])


def _flat(value) -> str:
    return "; ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)


CSV_COLUMNS = [
    "ChangeNumber", "Repository", "Environment", "DeploymentId", "WorkflowRunUrl", "DeploymentTime",
    "DeploymentStatus", "FailureCategory", "RootCause", "ErrorSummary", "FailedAttempt", "FailedJobs",
    "FailedSteps", "PodLogJob", "WorkflowChangeNumbers", "ChgSource", "Owner", "Technology",
    "SourceJob", "Evidence", "RecommendedAction", "DataQualityNotes", "CollectionErrorCount",
]
ERROR_COLUMNS = ["ChangeNumber", "Repository", "DeploymentId", "WorkflowUrl", "ApiEndpoint", "ErrorMessage"]


def write_summary_markdown(path, args, environment, stats, records, error_rows, top_repos, top_owners, top_techs) -> None:
    with open(path, "w", encoding="utf-8") as h:
        h.write("# 📊 Deployment Failure Analytics Report\n\n")
        h.write("## Filters Applied\n\n")
        h.write(f"- Environment: `{environment}`\n- Date Range: `{args.start_date}` to `{args.end_date}`\n")
        h.write("- Scope: failed PRD deployments of repositories that had unsuccessful ServiceNow CHGs\n\n")

        h.write("## Summary Statistics\n\n")
        for label, key in (
            ("Unsuccessful CHGs in ServiceNow dashboard", "snow_changes"),
            ("Unique repositories discovered", "repositories"),
            ("Failed PRD deployments for the repositories", "failed_deployments"),
        ):
            h.write(f"- {label}: `{stats[key]}`\n")
        h.write("\n")

        for title, rows, header in (
            ("Top Repositories", top_repos, ("Repository", "Failed deployments")),
            ("Top Failure Owners", top_owners, ("Owner", "Failed deployments")),
            ("Top Failure Technologies", top_techs, ("Technology", "Failed deployments")),
        ):
            h.write(f"## {title}\n\n| {header[0]} | {header[1]} | Percentage |\n|---|---|---|\n")
            for value, count, pct in rows or [("_none_", 0, 0.0)]:
                h.write(f"| {_md(value)} | {count} | {pct:.1f}% |\n")
            h.write("\n")

        h.write("## Summary Report\n\n")
        h.write("| CHG Number | Repository | Workflow Run | Deployment Time | Root Cause | Error Summary |\n")
        h.write("|---|---|---|---|---|---|\n")
        for r in records:
            link = f"[Open Run]({r['WorkflowRunUrl']})" if r["WorkflowRunUrl"].startswith("http") else "N/A"
            h.write(f"| {r['ChangeNumber']} | {_md(r['Repository'])} | {link} | "
                    f"{r['DeploymentTime']} | {_md(r['RootCause'])} | {_md(r['ErrorSummary'])} |\n")
        if not records:
            h.write("| N/A | - | - | - | - | - |\n")
        h.write("\n")

        h.write("## Collection Errors\n\n")
        if error_rows:
            h.write("| CHG Number | Repository | Deployment ID | Workflow URL | API Endpoint | Error Message |\n|---|---|---|---|---|---|\n")
            for e in error_rows:
                h.write("| " + " | ".join(_md(e[c], 300) for c in ERROR_COLUMNS) + " |\n")
        else:
            h.write("_No collection errors._\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a PRD deployment failure report: ServiceNow repositories -> GitHub failed deployments -> logs -> RCA")
    parser.add_argument("--environment", required=True, help="Environment name (prd, uat, unt, prf)")
    parser.add_argument("--status", default="failure", help="Kept for compatibility; only 'failure' is analyzed")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--snow-url", default=os.environ.get("SNOW_URL", ""), help="ServiceNow instance URL")
    parser.add_argument("--snow-requested-by", default=os.environ.get("SNOW_REQUESTED_BY", SNOW_DEFAULT_REQUESTED_BY))
    parser.add_argument("--snow-assignment-group", default=os.environ.get("SNOW_ASSIGNMENT_GROUP", SNOW_DEFAULT_ASSIGNMENT_GROUP))
    args = parser.parse_args()

    environment = args.environment.strip()
    if args.status.strip().lower() != "failure":
        print(f"[warn] --status {args.status} ignored: this report analyzes failed deployments only", file=sys.stderr)
    if not args.snow_url:
        print("Missing --snow-url (or SNOW_URL env var)", file=sys.stderr)
        return 1
    snow_api_key = os.environ.get("SNOW_PASSWORD", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not snow_api_key:
        print("Missing SNOW_PASSWORD", file=sys.stderr)
        return 1
    if not github_token:
        print("Missing GITHUB_TOKEN (required to query the GitHub Deployments API)", file=sys.stderr)
        return 1
    try:
        range_start = datetime.strptime(args.start_date, "%Y-%m-%d")
        range_end = datetime.strptime(args.end_date, "%Y-%m-%d")
    except ValueError:
        print("[error] --start-date/--end-date must be YYYY-MM-DD", file=sys.stderr)
        return 1

    os.makedirs(args.output_dir, exist_ok=True)
    started_at = time.time()
    print(f"[info] Environment: {environment} | Date range: {args.start_date} to {args.end_date}", flush=True)

    # 1-3. ServiceNow unsuccessful CHGs -> unique repository names.
    snow_client = ServiceNowClient(args.snow_url, snow_api_key)
    try:
        snow_records = fetch_snow_change_requests(snow_client, args.start_date, args.end_date, args.snow_requested_by, args.snow_assignment_group)
    except Exception as exc:  # noqa: BLE001 - without ServiceNow there is nothing to report on
        print(f"[error] ServiceNow query failed: {exc}", file=sys.stderr)
        return 1
    print(f"[info] ServiceNow returned {len(snow_records)} failed change request(s)", flush=True)

    chg_repos = [(snow_field(r, "number") or "N/A", extract_snow_repository(r)) for r in snow_records]
    repos = sorted({repo for _, repo in chg_repos if repo})
    without_repo = [number for number, repo in chg_repos if not repo]
    print(f"[info] {len(repos)} unique repositories from ServiceNow ({len(without_repo)} CHG(s) without a repository)", flush=True)
    if without_repo:
        print(f"[warn] CHGs without a repository: {', '.join(without_repo)}", file=sys.stderr)

    # GitHub Deployments API: failed PRD deployments of those repositories within the date range.
    errors = CollectionErrors()
    github_client = GitHubClient(github_token)
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_REPO_WORKERS) as executor:
        futures = {
            executor.submit(process_repository, github_client, errors, repo, environment, range_start, range_end): repo
            for repo in repos
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            repo = futures[future]
            try:
                records.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - record and continue
                errors.add({"chg": "N/A", "repository": repo}, "process_repository", f"{type(exc).__name__}: {exc}")
            if completed == 1 or completed % 5 == 0 or completed == len(futures):
                print(f"[progress] Repositories: {completed}/{len(futures)}", flush=True)

    print("[info] Enriching repository topics...", flush=True)
    enrich_records_with_topics(records, github_client, errors)

    error_rows = errors.rows()
    for r in records:
        r["CollectionErrorCount"] = _error_count(r, error_rows)
    records.sort(key=lambda r: (r["DeploymentDate"], r["DeploymentId"]), reverse=True)

    failed = [r for r in records if r["DeploymentStatus"] == "Failure"]
    stats = {
        "snow_changes": len(snow_records),
        "repositories": len(repos),
        "failed_deployments": len(failed),
        "chg_found": sum(1 for r in failed if r["ChangeNumber"] != "N/A"),
        "chg_not_found": sum(1 for r in failed if r["ChangeNumber"] == "N/A"),
        "collection_errors": len(error_rows),
    }
    top_repos = rank_by_field(records, "Repository")
    top_owners = rank_by_field(records, "Owner")
    top_techs = rank_by_field(records, "Technology")
    elapsed = time.time() - started_at

    csv_path = os.path.join(args.output_dir, "deployment-report.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as h:
        writer = csv.DictWriter(h, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in records:
            writer.writerow({c: _flat(r[c]) for c in CSV_COLUMNS})
    errors_path = os.path.join(args.output_dir, "collection-errors.csv")
    with open(errors_path, "w", encoding="utf-8", newline="") as h:
        writer = csv.DictWriter(h, fieldnames=ERROR_COLUMNS)
        writer.writeheader()
        writer.writerows(error_rows)

    json_path = os.path.join(args.output_dir, "deployment-report.json")
    with open(json_path, "w", encoding="utf-8") as h:
        json.dump(
            {
                "filters": {"environment": environment, "start_date": args.start_date, "end_date": args.end_date},
                "statistics": stats,
                "execution": {
                    "elapsed_seconds": round(elapsed, 1),
                    "servicenow_api_calls": snow_client.calls,
                    "github_api_calls": github_client.calls,
                },
                "top_repositories": [{"repository": v, "count": c} for v, c, _ in top_repos],
                "top_failure_owners": [{"owner": v, "failed_deployments": c, "percentage": round(p, 1)} for v, c, p in top_owners],
                "top_failure_technologies": [{"technology": v, "failed_deployments": c, "percentage": round(p, 1)} for v, c, p in top_techs],
                "deployments": records,
                "collection_errors": error_rows,
            },
            h, indent=2,
        )

    summary_path = os.path.join(args.output_dir, "summary.md")
    write_summary_markdown(summary_path, args, environment, stats, records, error_rows, top_repos, top_owners, top_techs)

    print(f"[info] Wrote {csv_path}, {errors_path}, {json_path}, {summary_path}", flush=True)
    print(f"[info] Done in {elapsed:.1f}s ({snow_client.calls} ServiceNow / {github_client.calls} GitHub API calls, {len(error_rows)} collection error(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
