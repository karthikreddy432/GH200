name: Archived Repository Review and Delete

on:
  schedule:
    - cron: '0 12 1 * *' # first day of every month at 12:00 UTC (6:00 AM CST)
  workflow_dispatch:
    inputs:
      organizations:
        description: 'Comma-separated organizations to scan'
        required: false
        default: 'sndr-core,sndr-secure'
        type: string
      age_days:
        description: 'Age threshold in days based on pushed_at'
        required: false
        default: '90'
        type: string
      repos_to_delete:
        description: 'Optional: comma/newline-separated org/repo values to delete specifically'
        required: false
        type: string
      tech_lead_email:
        description: 'Tech lead email for approval'
        required: false
        default: 'ITReleaseAutomationGroup@schneider.com'
        type: string

jobs:
  discover:
    runs-on: aks-runner-prd
    outputs:
      organizations: ${{ steps.resolve.outputs.organizations }}
      age_days: ${{ steps.resolve.outputs.age_days }}
      eligible_count: ${{ steps.discover.outputs.eligible_count }}
      selected_count: ${{ steps.discover.outputs.selected_count }}

    steps:
      - name: Resolve params
        id: resolve
        shell: bash
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            organizations="${{ github.event.inputs.organizations }}"
            age_days="${{ github.event.inputs.age_days }}"
          else
            organizations="sndr-core,sndr-secure"
            age_days="90"
          fi

          organizations=$(echo "$organizations" | tr '\n' ',' | sed 's/[[:space:]]//g' | sed 's/,,*/,/g' | sed 's/^,//;s/,$//')
          if [ -z "$organizations" ]; then
            organizations="sndr-core,sndr-secure"
          fi

          if ! [[ "$age_days" =~ ^[0-9]+$ ]]; then
            echo "Invalid age_days: $age_days"
            exit 1
          fi

          echo "organizations=$organizations" >> "$GITHUB_OUTPUT"
          echo "age_days=$age_days" >> "$GITHUB_OUTPUT"

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.11'

      - name: Discover and report
        id: discover
        env:
          GITHUB_TOKEN: ${{ secrets.GHEC_TOKEN }}
          ORGANIZATIONS: ${{ steps.resolve.outputs.organizations }}
          AGE_DAYS: ${{ steps.resolve.outputs.age_days }}
          REPOS_TO_DELETE: ${{ github.event.inputs.repos_to_delete }}
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p reports

          python - <<'PY'
          import csv
          import json
          import os
          import sys
          from datetime import datetime, timezone
          from urllib import error, request

          token = os.environ.get("GITHUB_TOKEN", "").strip()
          organizations_raw = os.environ.get("ORGANIZATIONS", "sndr-core,sndr-secure")
          age_days = int(os.environ.get("AGE_DAYS", "90"))
          repos_raw = os.environ.get("REPOS_TO_DELETE", "")
          github_output = os.environ.get("GITHUB_OUTPUT")

          if not token:
              print("Missing GHEC_TOKEN", file=sys.stderr)
              sys.exit(1)

          organizations = [org.strip() for org in organizations_raw.split(",") if org.strip()]
          if not organizations:
              organizations = ["sndr-core", "sndr-secure"]

          requested_full_names = [
              item.strip()
              for item in repos_raw.replace("\r", "\n").replace(",", "\n").split("\n")
              if item.strip()
          ]
          requested_set = set(requested_full_names)
          now = datetime.now(timezone.utc)

          def gh_request(url, method="GET"):
              req = request.Request(url=url, method=method)
              req.add_header("Authorization", f"Bearer {token}")
              req.add_header("Accept", "application/vnd.github+json")
              req.add_header("X-GitHub-Api-Version", "2022-11-28")
              try:
                  with request.urlopen(req, timeout=60) as resp:
                      return resp.getcode(), resp.read().decode("utf-8")
              except error.HTTPError as http_err:
                  return http_err.code, http_err.read().decode("utf-8", errors="replace")

          candidates = []
          org_scan_counts = {}

          for org in organizations:
              page = 1
              scanned = 0
              while True:
                  url = f"https://api.github.com/orgs/{org}/repos?type=all&per_page=100&page={page}"
                  status, body = gh_request(url)
                  if status != 200:
                      print(f"Failed listing repos for {org}, page {page}. HTTP {status}", file=sys.stderr)
                      print(body, file=sys.stderr)
                      sys.exit(1)

                  repos = json.loads(body)
                  if not repos:
                      break

                  scanned += len(repos)
                  for repo in repos:
                      if not repo.get("archived", False):
                          continue

                      pushed_at = repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")
                      if not pushed_at:
                          continue

                      pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                      age = (now - pushed_dt).days
                      if age <= age_days:
                          continue

                      # Extract owner and technology topics from repo topics
                      topics = repo.get("topics", []) or []
                      owner_value = ""
                      tech_topics = []
                      for topic in topics:
                          if topic.startswith("owner-"):
                              owner_value = topic.replace("owner-", "")
                          elif topic.startswith("technology-"):
                              tech_topics.append(topic.replace("technology-", ""))  # Strip "technology-" prefix

                      candidates.append(
                          {
                              "org": org,
                              "repo": repo.get("name", ""),
                              "owner": owner_value,
                              "technology_topic": ", ".join(tech_topics),
                              "archived_reference_date": pushed_at,
                              "age_days": age,
                              "full_name": repo.get("full_name", ""),
                              "html_url": repo.get("html_url", ""),
                          }
                      )

                  page += 1

              org_scan_counts[org] = scanned

          candidates.sort(key=lambda row: (row["org"], -row["age_days"], row["repo"]))
          for index, row in enumerate(candidates, start=1):
              row["s_no"] = index

          eligible_lookup = {row["full_name"]: row for row in candidates}
          excluded_repositories = []
          if requested_set:
              selected_candidates = []
              seen_selected = set()
              for full_name in requested_full_names:
                  candidate = eligible_lookup.get(full_name)
                  if candidate is None:
                      excluded_repositories.append({"full_name": full_name, "reason": "not-eligible-for-delete"})
                      continue
                  if full_name in seen_selected:
                      continue
                  seen_selected.add(full_name)
                  selected_candidates.append(candidate)
          else:
              selected_candidates = list(candidates)

          with open("reports/eligible-repos.csv", "w", encoding="utf-8", newline="") as handle:
              writer = csv.DictWriter(
                  handle,
                  fieldnames=[
                      "s_no",
                      "org",
                      "repo",
                      "owner",
                      "technology_topic",
                      "archived_reference_date",
                      "age_days",
                      "full_name",
                      "html_url",
                  ],
              )
              writer.writeheader()
              writer.writerows(candidates)

          with open("reports/selected-repos.csv", "w", encoding="utf-8", newline="") as handle:
              writer = csv.DictWriter(
                  handle,
                  fieldnames=[
                      "s_no",
                      "org",
                      "repo",
                      "owner",
                      "technology_topic",
                      "archived_reference_date",
                      "age_days",
                      "full_name",
                      "html_url",
                  ],
              )
              writer.writeheader()
              writer.writerows(selected_candidates)

          with open("reports/selected-repos.json", "w", encoding="utf-8") as handle:
              json.dump(selected_candidates, handle, indent=2)

          with open("reports/excluded-repos.csv", "w", encoding="utf-8", newline="") as handle:
              writer = csv.DictWriter(handle, fieldnames=["full_name", "reason"])
              writer.writeheader()
              writer.writerows(excluded_repositories)

          with open("reports/summary.md", "w", encoding="utf-8") as handle:
              handle.write("## Archived Repository Candidate Report\n\n")
              handle.write(f"- Organizations: `{', '.join(organizations)}`\n")
              handle.write(f"- Age threshold (days): `{age_days}`\n")
              handle.write(f"- Total candidates: `{len(candidates)}`\n")
              handle.write(f"- Total selected for deletion: `{len(selected_candidates)}`\n")
              handle.write(f"- Generated at (UTC): `{now.isoformat()}`\n")
              handle.write("- Date reference: `archived_reference_date` uses `pushed_at` (GitHub API does not provide `archived_at`)\n\n")

              handle.write("### Per-organization scan counts\n\n")
              for org in organizations:
                  handle.write(f"- {org}: scanned `{org_scan_counts.get(org, 0)}` repos\n")

              handle.write("\n### Candidate repositories (first 200)\n\n")
              if not candidates:
                  handle.write("No archived repositories older than the configured threshold were found.\n")
              else:
                  handle.write("| S.NO | Org | Repo Name | Owner | Technology Topic | Archived Date (Reference) | Age (days) | Link |\n")
                  handle.write("|---:|---|---|---|---|---|---:|---|\n")
                  for row in candidates[:200]:
                      handle.write(
                          f"| {row['s_no']} | {row['org']} | {row['repo']} | {row['owner']} | {row['technology_topic']} | {row['archived_reference_date']} | {row['age_days']} | [open]({row['html_url']}) |\n"
                      )

              handle.write("\n### Repositories selected for mark and delete\n\n")
              if not selected_candidates:
                  handle.write("No repositories were selected for deletion in this run.\n")
              else:
                  handle.write("| S.NO | Org | Repo Name | Archived Age (days) | Link |\n")
                  handle.write("|---:|---|---|---:|---|\n")
                  for row in selected_candidates[:200]:
                      handle.write(
                          f"| {row['s_no']} | {row['org']} | {row['repo']} | {row['age_days']} | [open]({row['html_url']}) |\n"
                      )

                  if excluded_repositories:
                      handle.write("\n### Requested repositories excluded from deletion\n\n")
                      handle.write("| Repository | Reason |\n")
                      handle.write("|---|---|\n")
                      for row in excluded_repositories:
                          handle.write(f"| {row['full_name']} | {row['reason']} |\n")

              handle.write("\n### Approval and deletion flow\n\n")
              handle.write("1. Repositories marked with topic `mark-for-delete` awaiting approval.\n")
              handle.write("2. An approval email with the selected repository summary has been sent.\n")
              handle.write("3. The workflow will wait for `delete-approval` environment approval.\n")
              handle.write("4. The delete job revalidates archive state, age threshold, and `mark-for-delete` topic before deletion.\n")

          if github_output:
              with open(github_output, "a", encoding="utf-8") as handle:
                  handle.write(f"eligible_count={len(candidates)}\n")
                  handle.write(f"selected_count={len(selected_candidates)}\n")
          PY

      - name: Upload reports
        uses: actions/upload-artifact@v7
        with:
          name: discovery-reports
          path: reports/
          retention-days: 7

  # ===========================================================================
  # CHANGED: validate_deployments
  # Checks whether each archived repo still has a live workload in AKS/OCP.
  #   - Namespace comes from the repo topic  ns-<name>
  #   - Cluster comes from topics: aksspoke2 / aksenclave / akshub / ocp|openshift
  #   - Environments are checked in order and checking STOPS at the first hit:
  #       AKS: prd -> uat -> unt        OCP: prd -> uat -> prf
  #   - Only repos with NO active deployment end up in validated-repos.json
  #   - Repos that cannot be validated (auth/network/API errors) are blocked
  #     (fail safe) and never reach mark_for_delete
  # Outputs below are what the downstream jobs use for their `if:` conditions.
  # ===========================================================================
  validate_deployments:
    name: Validate Active Deployments
    needs: discover
    runs-on: aks-runner-prd
    outputs:
      validated_count: ${{ steps.validate.outputs.validated_count }}
      active_count: ${{ steps.validate.outputs.active_count }}
      error_count: ${{ steps.validate.outputs.error_count }}

    steps:
      - name: Download discovery reports
        uses: sndr-core/ra-workflows/actions/common/download-artifact@master
        with:
          Name: discovery-reports
          Path: reports/

      - name: Setup Python
        uses: actions/setup-python@v6
        with:
          python-version: '3.11'

      # Reads topics of every selected repo and derives namespace + cluster type.
      # Topic/cluster detection logic is unchanged from the original workflow.
      - name: Build repo metadata
        env:
          GITHUB_TOKEN: ${{ secrets.GHEC_TOKEN }}
        shell: bash
        run: |
          python <<'PY'
          import json
          import urllib.request
          import os

          token = os.environ["GITHUB_TOKEN"]

          with open("reports/selected-repos.json") as f:
              repos = json.load(f)

          enriched = []

          for repo in repos:

              full_name = repo["full_name"]

              req = urllib.request.Request(
                  f"https://api.github.com/repos/{full_name}/topics"
              )

              req.add_header("Authorization", f"Bearer {token}")
              req.add_header("Accept", "application/vnd.github+json")

              topics = []
              repo.pop("metadata_error", None)

              # CHANGED: added timeout=60 and the metadata_error marker below

              try:
                  response = urllib.request.urlopen(req, timeout=60)
                  topics = json.loads(
                      response.read().decode()
                  ).get("names", [])
              except Exception as exc:
                  # Fail safe: a repo whose topics cannot be read must NOT be
                  # treated as "no deployment" - it is excluded downstream.
                  repo["metadata_error"] = f"{type(exc).__name__}: {exc}"

              namespace = None
              cluster_type = None

              for topic in topics:
                  if topic.startswith("ns-"):
                      namespace = topic[3:]
                      break

              if any("aksspoke2" in t.lower() for t in topics):
                  cluster_type = "aks-spoke"
              elif any("aksenclave" in t.lower() for t in topics):
                  cluster_type = "aks-enclave"
              elif any("akshub" in t.lower() for t in topics):
                  cluster_type = "aks-hub"
              elif any(
                  "ocp" in t.lower() or "openshift" in t.lower()
                  for t in topics
              ):
                  cluster_type = "ocp"

              repo["namespace"] = namespace
              repo["cluster_type"] = cluster_type

              repo["unt"] = "UNKNOWN"
              repo["prf"] = "UNKNOWN"
              repo["uat"] = "UNKNOWN"
              repo["prd"] = "UNKNOWN"

              enriched.append(repo)

          with open(
              "reports/repos-for-validation.json",
              "w"
          ) as f:
              json.dump(enriched, f, indent=2)

          print(f"Prepared {len(enriched)} repositories")
          PY

      - name: Install jq
        run: |
          which jq || sudo yum install -y jq || true

      - name: Setup kubectl
        uses: azure/setup-kubectl@v4
        with:
          version: ${{ vars.KUBECTL_VERSION }}

      - name: Install OC Client
        uses: sndr-core/ra-workflows/actions/ocp/install-oc-client@master

      # CHANGED (new): OpenShift authentication, same pattern as the existing
      # "Get Resource Status" workflow (Set OpenShift variables -> Log in).
      # Two logins are needed because OCP prd lives on the PRD server while
      # OCP uat + prf live on the NONPRD server, and both are checked in one job.
      # Each login is verified with `oc whoami` BEFORE validation starts, and the
      # resulting oc context name is exposed as a step output.

      # ---- OpenShift non-prod (serves prf + uat) -------------------------
      - name: Set OpenShift variables (non-prod)
        run: |
          echo "OPENSHIFT_SERVER=${{ vars.OPENSHIFT_SERVER_NONPRD }}" >> "$GITHUB_ENV"
          echo "OPENSHIFT_TOKEN=${{ secrets.OPENSHIFT_TOKEN }}" >> "$GITHUB_ENV"

      - name: Log in to OpenShift (non-prod)
        uses: sndr-core/ra-workflows/actions/ocp/login-to-openshift@master
        with:
          openshift_server_url: ${{ env.OPENSHIFT_SERVER }}
          openshift_token: ${{ env.OPENSHIFT_TOKEN }}
          certificate_authority_data: ${{ secrets.SCHNEIDER_SHA2_CA2 }}
          namespace: default

      - name: Verify OpenShift login (non-prod)
        id: ocp_nonprd
        run: |
          # Fails the job here if the login did not work; saves the context name
          ctx="$(oc config current-context)"
          oc --context "$ctx" whoami
          echo "context=$ctx" >> "$GITHUB_OUTPUT"

      # ---- OpenShift prod (serves prd) -----------------------------------
      - name: Set OpenShift variables (prod)
        run: |
          echo "OPENSHIFT_SERVER=${{ vars.OPENSHIFT_SERVER_PRD }}" >> "$GITHUB_ENV"
          echo "OPENSHIFT_TOKEN=${{ secrets.OPENSHIFT_TOKEN_PRD }}" >> "$GITHUB_ENV"

      - name: Log in to OpenShift (prod)
        uses: sndr-core/ra-workflows/actions/ocp/login-to-openshift@master
        with:
          openshift_server_url: ${{ env.OPENSHIFT_SERVER }}
          openshift_token: ${{ env.OPENSHIFT_TOKEN }}
          certificate_authority_data: ${{ secrets.SCHNEIDER_SHA2_CA2 }}
          namespace: default

      - name: Verify OpenShift login (prod)
        id: ocp_prd
        run: |
          # Fails the job here if the login did not work; saves the context name
          ctx="$(oc config current-context)"
          oc --context "$ctx" whoami
          echo "context=$ctx" >> "$GITHUB_OUTPUT"

      # CHANGED: rewritten validation step (valid YAML indentation, sibling step
      # after "Install OC Client"). Writes reports/validated-repos.json,
      # reports/active-deployments.csv and reports/deployment-validation-summary.md
      - name: Validate deployments and filter repositories
        id: validate
        shell: bash
        env:
          # oc contexts created by the two login steps above
          OCP_CTX_NONPRD: ${{ steps.ocp_nonprd.outputs.context }}
          OCP_CTX_PRD: ${{ steps.ocp_prd.outputs.context }}
          # AKS kubeconfigs are passed via env (not inlined in the script)
          AKS_UNT4_KUBECONFIG: ${{ secrets.AKS_UNT4_KUBECONFIG }}
          AKS_UAT4_KUBECONFIG: ${{ secrets.AKS_UAT4_KUBECONFIG }}
          AKS_PRD4_KUBECONFIG: ${{ secrets.AKS_PRD4_KUBECONFIG }}
          AKS_UNT4_ENC_KUBECONFIG: ${{ secrets.AKS_UNT4_ENC_KUBECONFIG }}
          AKS_UAT4_ENC_KUBECONFIG: ${{ secrets.AKS_UAT4_ENC_KUBECONFIG }}
          AKS_PRD4_ENC_KUBECONFIG: ${{ secrets.AKS_PRD4_ENC_KUBECONFIG }}
          AKS_PRD4_HUB_KUBECONFIG: ${{ secrets.AKS_PRD4_HUB_KUBECONFIG }}
        run: |
          set -euo pipefail
          umask 077
          mkdir -p reports

          # Decode a kubeconfig secret. An empty/undecodable secret yields an
          # empty file; the validator then reports that environment as an
          # ERROR (repo excluded) instead of "no deployment".
          decode_kubeconfig() {
            local target="$1" value="$2"
            if ! printf '%s' "$value" | base64 --decode > "$target" 2>/dev/null; then
              echo "::warning::Could not decode kubeconfig for $target"
              : > "$target"
            fi
          }

          # AKS spoke kubeconfigs
          decode_kubeconfig /tmp/aks-unt "$AKS_UNT4_KUBECONFIG"
          decode_kubeconfig /tmp/aks-uat "$AKS_UAT4_KUBECONFIG"
          decode_kubeconfig /tmp/aks-prd "$AKS_PRD4_KUBECONFIG"

          # AKS enclave kubeconfigs
          decode_kubeconfig /tmp/aks-enc-unt "$AKS_UNT4_ENC_KUBECONFIG"
          decode_kubeconfig /tmp/aks-enc-uat "$AKS_UAT4_ENC_KUBECONFIG"
          decode_kubeconfig /tmp/aks-enc-prd "$AKS_PRD4_ENC_KUBECONFIG"

          # AKS hub kubeconfig (prd only; hub uat/unt use the spoke kubeconfigs,
          # same as the "Get Pod/Resource Status" workflows)
          decode_kubeconfig /tmp/aks-hub-prd "$AKS_PRD4_HUB_KUBECONFIG"

          python <<'PY'
          import csv
          import json
          import os
          import re
          import subprocess

          with open("reports/repos-for-validation.json") as f:
              repos = json.load(f)

          # Namespace topics come from GitHub; only allow valid k8s names so they
          # can never be used for command injection.
          NS_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

          # oc contexts: "nonprd" server serves uat + prf, "prd" server serves prd
          OCP_CONTEXTS = {
              "nonprd": os.environ.get("OCP_CTX_NONPRD", "").strip(),
              "prd": os.environ.get("OCP_CTX_PRD", "").strip(),
          }

          # Resource types that count as an "active deployment"
          AKS_RESOURCES = ["deployments", "statefulsets", "cronjobs"]
          OCP_RESOURCES = ["deployments", "dc", "statefulsets", "cronjobs"]  # dc = DeploymentConfig

          validated = []   # safe to continue to mark_for_delete
          active = []      # active deployment found -> blocked
          errors = []      # could not be validated -> blocked (fail safe)

          # Runs a command WITHOUT a shell (no injection) with a 60s timeout.
          def run(cmd):
              try:
                  r = subprocess.run(
                      cmd,
                      capture_output=True,
                      text=True,
                      timeout=60
                  )
                  return r.returncode, r.stdout, r.stderr
              except Exception as exc:
                  return 1, "", f"{type(exc).__name__}: {exc}"

          # Returns None when the kubeconfig is missing/empty (secret not set or
          # not decodable) -> reported as "credentials unavailable" (blocked).
          def aks_cmd(kubeconfig):
              if not os.path.isfile(kubeconfig) or os.path.getsize(kubeconfig) == 0:
                  return None
              return ["kubectl", "--kubeconfig", kubeconfig]

          # Returns None when the oc login context is unavailable.
          def ocp_cmd(scope):
              ctx = OCP_CONTEXTS[scope]
              if not ctx:
                  return None
              return ["oc", "--context", ctx]

          def check_resources(base_cmd, namespace, resources):
              """Returns ("active", resource) | ("none", "") | ("error", detail).

              A missing namespace returns an empty list (rc 0), i.e. "none".
              Any command failure (auth, network, RBAC, timeout) is "error",
              never "none", so a broken connection cannot make a repo deletable.
              """
              problem = ""
              for resource in resources:
                  rc, out, err = run(
                      base_cmd + ["get", resource, "-n", namespace, "--no-headers"]
                  )

                  if rc == 0:
                      if out.strip():
                          return "active", resource
                      continue

                  # e.g. cluster without DeploymentConfig API: not an error
                  if (
                      "doesn't have a resource type" in err
                      or "the server could not find the requested resource" in err
                  ):
                      continue  # API type not served on this cluster

                  problem = f"{resource}: {err.strip()[:200]}"

              if problem:
                  return "error", problem
              return "none", ""

          # Builds the ordered list of environments to check for one repo.
          # ORDER MATTERS: production first so the most important hit is found
          # early; the caller stops at the first environment with a deployment.
          def build_plan(cluster_type, namespace):
              # (environment, namespace, command prefix, resources) - in check order
              if cluster_type == "aks-spoke":
                  return [
                      (env, f"{env}-{namespace}", aks_cmd(f"/tmp/aks-{env}"), AKS_RESOURCES)
                      for env in ("prd", "uat", "unt")
                  ]

              if cluster_type == "aks-enclave":
                  return [
                      (env, f"{env}-{namespace}", aks_cmd(f"/tmp/aks-enc-{env}"), AKS_RESOURCES)
                      for env in ("prd", "uat", "unt")
                  ]

              if cluster_type == "aks-hub":
                  # Hub prd has its own kubeconfig; hub uat/unt use the spoke
                  # kubeconfigs, same as the Get Pod/Resource Status workflows.
                  hub_kubeconfigs = {
                      "prd": "/tmp/aks-hub-prd",
                      "uat": "/tmp/aks-uat",
                      "unt": "/tmp/aks-unt",
                  }
                  return [
                      (env, f"{env}-{namespace}", aks_cmd(hub_kubeconfigs[env]), AKS_RESOURCES)
                      for env in ("prd", "uat", "unt")
                  ]

              if cluster_type == "ocp":
                  # prf and uat -> non-prod OpenShift server, prd -> prod server
                  ocp_scope = {"prd": "prd", "uat": "nonprd", "prf": "nonprd"}
                  return [
                      (env, f"{env}-{namespace}", ocp_cmd(ocp_scope[env]), OCP_RESOURCES)
                      for env in ("prd", "uat", "prf")
                  ]

              return []

          # ---- Main loop: every repo ends up in exactly ONE of the 3 lists ----
          for repo in repos:

              namespace = repo.get("namespace")
              cluster_type = repo.get("cluster_type")
              repo_name = repo.get("repo")
              full_name = repo.get("full_name")

              # Topics could not be read -> cannot prove it is unused -> blocked
              if repo.get("metadata_error"):
                  repo["validation_status"] = "error"
                  errors.append({
                      "repo": repo_name,
                      "full_name": full_name,
                      "reason": f"topics-fetch-failed: {repo['metadata_error']}"
                  })
                  continue

              if not namespace or not cluster_type:
                  # No ns-*/cluster topic: nothing to validate (existing behaviour)
                  repo["validation_status"] = "unvalidated-no-topic-metadata"
                  validated.append(repo)
                  continue

              if not NS_PATTERN.match(namespace):
                  repo["validation_status"] = "error"
                  errors.append({
                      "repo": repo_name,
                      "full_name": full_name,
                      "reason": f"invalid-namespace-topic: {namespace}"
                  })
                  continue

              found = None
              problems = []

              # Check environments in order; stop at the first deployment found
              for env_name, ns, cmd, resources in build_plan(cluster_type, namespace):

                  if cmd is None:
                      repo[env_name] = "ERROR"
                      problems.append(f"{env_name}: credentials unavailable")
                      continue

                  state, detail = check_resources(cmd, ns, resources)

                  if state == "active":
                      repo[env_name] = "ACTIVE"
                      found = {
                          "repo": repo_name,
                          "full_name": full_name,
                          "namespace": ns,
                          "environment": env_name,
                          "cluster_type": cluster_type,
                          "resource_type": detail
                      }
                      break  # stop checking further environments

                  if state == "error":
                      repo[env_name] = "ERROR"
                      problems.append(f"{env_name}: {detail}")
                  else:
                      repo[env_name] = "NONE"

              # Decision: active -> blocked, errors -> blocked, otherwise validated
              if found:
                  repo["validation_status"] = "active-deployment"
                  active.append(found)
              elif problems:
                  repo["validation_status"] = "error"
                  errors.append({
                      "repo": repo_name,
                      "full_name": full_name,
                      "reason": "; ".join(problems)
                  })
              else:
                  repo["validation_status"] = "no-active-deployment"
                  validated.append(repo)

          # Hard guarantees before anything is written:
          #  1) no repo can be both validated and active/errored
          #  2) every input repo is accounted for
          validated_names = {r["full_name"] for r in validated}
          blocked_names = {a["full_name"] for a in active} | {e["full_name"] for e in errors}
          if validated_names & blocked_names:
              raise SystemExit("Safety check failed: active/errored repo present in validated list")
          if len(validated) + len(active) + len(errors) != len(repos):
              raise SystemExit("Safety check failed: repository accounting mismatch")

          # Output 1: ONLY these repos may continue to mark_for_delete
          with open(
              "reports/validated-repos.json",
              "w"
          ) as f:
              json.dump(validated, f, indent=2)

          # Output 2: repos blocked because an active deployment was found
          with open(
              "reports/active-deployments.csv",
              "w",
              newline=""
          ) as f:
              writer = csv.DictWriter(
                  f,
                  fieldnames=[
                      "repo",
                      "full_name",
                      "namespace",
                      "environment",
                      "cluster_type",
                      "resource_type"
                  ]
              )
              writer.writeheader()
              writer.writerows(active)

          # Output 3: human-readable summary (also shown in the job summary)
          with open(
              "reports/deployment-validation-summary.md",
              "w"
          ) as f:

              f.write("# Deployment Validation Summary\n\n")
              f.write(f"- Input repositories: {len(repos)}\n")
              f.write(f"- Active deployments found (blocked): {len(active)}\n")
              f.write(f"- Could not be validated (blocked): {len(errors)}\n")
              f.write(f"- Safe to delete: {len(validated)}\n\n")

              if active:
                  f.write("## Active deployments (not marked for delete)\n\n")
                  f.write("| Repository | Environment | Namespace | Cluster | Resource |\n")
                  f.write("|---|---|---|---|---|\n")

                  for item in active:
                      f.write(
                          f"| {item['repo']} | "
                          f"{item['environment']} | "
                          f"{item['namespace']} | "
                          f"{item['cluster_type']} | "
                          f"{item['resource_type']} |\n"
                      )
                  f.write("\n")

              if errors:
                  f.write("## Validation errors (not marked for delete)\n\n")
                  f.write("| Repository | Reason |\n")
                  f.write("|---|---|\n")

                  for item in errors:
                      reason = item["reason"].replace("|", "/").replace("\n", " ")
                      f.write(f"| {item['repo']} | {reason} |\n")
                  f.write("\n")

              unvalidated = [
                  r for r in validated
                  if r.get("validation_status") == "unvalidated-no-topic-metadata"
              ]
              if unvalidated:
                  f.write("## Safe to delete without cluster check (no ns-*/cluster topic)\n\n")
                  for r in unvalidated:
                      f.write(f"- {r.get('full_name')}\n")
                  f.write("\n")

          # Job outputs used by the downstream `if:` conditions
          with open(os.environ["GITHUB_OUTPUT"], "a") as f:
              f.write(f"validated_count={len(validated)}\n")
              f.write(f"active_count={len(active)}\n")
              f.write(f"error_count={len(errors)}\n")

          print(f"Repositories analysed : {len(repos)}")
          print(f"Active deployments    : {len(active)}")
          print(f"Validation errors     : {len(errors)}")
          print(f"Safe to delete        : {len(validated)}")
          PY

          cat reports/deployment-validation-summary.md >> "$GITHUB_STEP_SUMMARY"

      # CHANGED (new): artifact consumed by mark_for_delete (instead of discovery-reports)
      - name: Upload validation reports
        uses: actions/upload-artifact@v7
        with:
          name: validation-reports
          path: reports/
          retention-days: 7

  # ===========================================================================
  # TESTING: mark_for_delete, notify and delete are commented out below.
  # To enable them, remove the leading "# " from every line from here to the
  # end of the file (keep the 2-space job indentation that follows the "# ").
  # ===========================================================================
#   # CHANGED: now depends on validate_deployments and only runs when at least
#   # one repo passed validation.
#   mark_for_delete:
#     needs: [discover, validate_deployments]
#     if: ${{ needs.validate_deployments.outputs.validated_count != '0' }}
#     runs-on: aks-runner-prd
# 
#     steps:
#       # CHANGED: downloads validation-reports (was discovery-reports)
#       - name: Download reports
#         uses: sndr-core/ra-workflows/actions/common/download-artifact@master
#         with:
#           Name: validation-reports
#           Path: reports/
# 
#       - name: Set up Python
#         uses: actions/setup-python@v6
#         with:
#           python-version: '3.11'
# 
#       - name: Add mark topic
#         id: mark
#         env:
#           GITHUB_TOKEN: ${{ secrets.GHEC_TOKEN }}
#         shell: bash
#         run: |
#           set -euo pipefail
# 
#           python - <<'PY'
#           import csv
#           import json
#           import os
#           import sys
#           from urllib import error, request
# 
#           token = os.environ.get("GITHUB_TOKEN", "").strip()
#           if not token:
#               print("Missing GHEC_TOKEN", file=sys.stderr)
#               sys.exit(1)
# 
#           topic_name = "mark-for-delete"
# 
#           def gh_request(url, method="GET", payload=None):
#               req = request.Request(url=url, method=method)
#               req.add_header("Authorization", f"Bearer {token}")
#               req.add_header("Accept", "application/vnd.github+json")
#               req.add_header("X-GitHub-Api-Version", "2022-11-28")
#               payload_bytes = None
#               if payload is not None:
#                   req.add_header("Content-Type", "application/json")
#                   payload_bytes = json.dumps(payload).encode("utf-8")
#               try:
#                   with request.urlopen(req, data=payload_bytes, timeout=60) as resp:
#                       return resp.getcode(), resp.read().decode("utf-8")
#               except error.HTTPError as http_err:
#                   return http_err.code, http_err.read().decode("utf-8", errors="replace")
# 
#           # CHANGED: reads validated-repos.json (was selected-repos.json)
#           with open("reports/validated-repos.json", "r", encoding="utf-8") as f:
#               selected_repos = json.load(f)
# 
#           # CHANGED (new): Defensive gate: a repo with an active deployment must never be marked
#           with open("reports/active-deployments.csv", "r", encoding="utf-8", newline="") as f:
#               active_names = {row["full_name"] for row in csv.DictReader(f)}
#           conflicts = sorted(r["full_name"] for r in selected_repos if r["full_name"] in active_names)
#           if conflicts:
#               print(f"Refusing to mark repositories with active deployments: {conflicts}", file=sys.stderr)
#               sys.exit(1)
# 
#           results = []
#           for row in selected_repos:
#               full_name = row["full_name"]
#               status, body = gh_request(f"https://api.github.com/repos/{full_name}/topics")
#               if status != 200:
#                   results.append(
#                       {
#                           "full_name": full_name,
#                           "topic_status": "failed",
#                           "reason": f"topics-fetch-http-{status}",
#                       }
#                   )
#                   continue
# 
#               current_topics = json.loads(body).get("names", []) or []
#               merged_topics = sorted(set(current_topics + [topic_name]))
#               put_status, _ = gh_request(
#                   f"https://api.github.com/repos/{full_name}/topics",
#                   method="PUT",
#                   payload={"names": merged_topics},
#               )
#               if put_status != 200:
#                   results.append(
#                       {
#                           "full_name": full_name,
#                           "topic_status": "failed",
#                           "reason": f"topics-put-http-{put_status}",
#                       }
#                   )
#                   continue
# 
#               row["topics_after_mark"] = ", ".join(merged_topics)
#               results.append(
#                   {
#                       "full_name": full_name,
#                       "topic_status": "already-present" if topic_name in current_topics else "added",
#                       "reason": "",
#                   }
#               )
# 
#           failures = [item for item in results if item["topic_status"] == "failed"]
# 
#           with open("reports/topic-mark-results.csv", "w", encoding="utf-8", newline="") as handle:
#               writer = csv.DictWriter(handle, fieldnames=["full_name", "topic_status", "reason"])
#               writer.writeheader()
#               writer.writerows(results)
# 
#           # CHANGED: written back as validated-repos.json (notify reads this file)
#           with open("reports/validated-repos.json", "w", encoding="utf-8") as handle:
#               json.dump(selected_repos, handle, indent=2)
# 
#           with open("reports/topic-mark-summary.md", "w", encoding="utf-8") as handle:
#               handle.write("## Topic Mark Summary\n\n")
#               handle.write("- Topic applied: `mark-for-delete`\n")
#               handle.write(f"- Selected repositories: `{len(selected_repos)}`\n")
#               handle.write(f"- Topic update failures: `{len(failures)}`\n\n")
#               handle.write("| Repository | Topic Status | Reason |\n")
#               handle.write("|---|---|---|\n")
#               for item in results:
#                   handle.write(f"| {item['full_name']} | {item['topic_status']} | {item['reason']} |\n")
# 
#           if failures:
#               print(f"Failed to mark {len(failures)} repositories with topic {topic_name}", file=sys.stderr)
#               sys.exit(1)
#           PY
# 
#       - name: Upload reports
#         uses: actions/upload-artifact@v7
#         with:
#           name: mark-reports
#           path: reports/
#           retention-days: 7
# 
#   # CHANGED: needs validate_deployments; condition uses validated_count
#   notify:
#     needs: [discover, validate_deployments, mark_for_delete]
#     if: ${{ needs.validate_deployments.outputs.validated_count != '0' }}
#     runs-on: aks-runner-prd
# 
#     steps:
#       - name: Download reports
#         uses: sndr-core/ra-workflows/actions/common/download-artifact@master
#         with:
#           Name: mark-reports
#           Path: reports/
# 
#       - name: Set up Python
#         uses: actions/setup-python@v6
#         with:
#           python-version: '3.11'
# 
#       - name: Prepare email
#         id: prepare
#         env:
#           TECH_LEAD_EMAIL: ${{ github.event.inputs.tech_lead_email || 'BrahmaiahB@schneider.com' }}
#           ORGANIZATIONS: ${{ needs.discover.outputs.organizations }}
#           AGE_DAYS: ${{ needs.discover.outputs.age_days }}
#         shell: bash
#         run: |
#           python - <<'PY'
#           import json
#           import os
#           import sys
# 
#           # CHANGED: reads validated-repos.json (was selected-repos.json) so the
#           # email only lists repos that were validated and marked
#           with open("reports/validated-repos.json", "r", encoding="utf-8") as f:
#               marked_repos = json.load(f)
#           tech_lead_email = os.environ.get("TECH_LEAD_EMAIL", "BrahmaiahB@schneider.com").strip()
#           organizations = os.environ.get("ORGANIZATIONS", "")
#           age_days = os.environ.get("AGE_DAYS", "")
#           github_output = os.environ.get("GITHUB_OUTPUT")
# 
#           # Format table data with pipe-delimited columns
#           summary_lines = []
#           # Header row
#           summary_lines.append("S.No|Organization|Repository|Owner|Technology Topics|Archived Date|Age (days)\n")
#           # Data rows
#           for idx, repo in enumerate(marked_repos, 1):
#               tech_topics = repo.get('technology_topic', '').replace('|', ',')
#               summary_lines.append(f"{idx}|{repo.get('org', '')}|{repo.get('repo', '')}|{repo.get('owner', '')}|{tech_topics}|{repo.get('archived_reference_date', '')}|{repo.get('age_days', '')}\n")
# 
#           resources_summary = "".join(summary_lines)
# 
#           if github_output:
#               with open(github_output, "a", encoding="utf-8") as handle:
#                   handle.write(f"tech_lead_email={tech_lead_email}\n")
#                   handle.write("resources_summary<<EOF\n")
#                   handle.write(resources_summary)
#                   handle.write("EOF\n")
#           PY
# 
#       - name: Send email
#         uses: sndr-core/ra-workflows/actions/common/archive/email_notification@feature/deletearchiverepo
#         with:
#           RESOURCES_SUMMARY: ${{ steps.prepare.outputs.resources_summary }}
#           CLIENT_SECRET_PROD: ${{ secrets.API_EMAIL_CLIENT_SECRET_PROD }}
#           SUBSCRIPTION_KEY_PROD: ${{ secrets.API_EMAIL_SUBCRIPTION_KEY_PROD }}
#           MAIL_TO: ${{ github.actor }}
#           Tech_Lead_Email: ${{ steps.prepare.outputs.tech_lead_email || github.actor }}
#           EMAIL_TITLE: '🗑️ Archived Repository Deletion Approval'
#           EMAIL_DESCRIPTION: 'Archived repositories have been marked for deletion and are awaiting approval.'
#           EMAIL_HEADER: 'ℹ️ <strong>NOTIFICATION:</strong> The archived repositories listed below have been marked with the <code>mark-for-delete</code> topic and are pending approval for deletion.'
#           TABLE_HEADER_TITLE: '📦 Repositories Marked for Deletion'
#         env:
#           GH_TOKEN: ${{ secrets.GHEC_TOKEN }}
# 
#       - name: Publish email summary
#         shell: python
#         env:
#           RESOURCES_SUMMARY: ${{ steps.prepare.outputs.resources_summary }}
#           # CHANGED: count comes from validation (was discover.selected_count)
#           SELECTED_COUNT: ${{ needs.validate_deployments.outputs.validated_count }}
#         run: |
#           import os
# 
#           resources_summary = os.environ.get('RESOURCES_SUMMARY', '').strip()
#           selected_count = os.environ.get('SELECTED_COUNT', '0')
# 
#           with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
#               f.write("## 🗑️ Archived Repository Deletion Approval\n\n")
#               f.write("Archived repositories have been marked for deletion and are awaiting approval.\n\n")
# 
#               if resources_summary:
#                 lines = resources_summary.split('\n')
#                 f.write("### Repositories Marked for Deletion\n\n")
# 
#                 # Process header
#                 if lines:
#                   headers = [h.strip() for h in lines[0].split('|')]
#                   f.write("| " + " | ".join(headers) + " |\n")
#                   f.write("|" + "|".join(["---"] * len(headers)) + "|\n")
# 
#                   # Process data rows
#                   for line in lines[1:]:
#                     line = line.strip()
#                     if line:
#                       cells = [c.strip() for c in line.split('|')]
#                       f.write("| " + " | ".join(cells) + " |\n")
# 
#               f.write(f"\n- Total marked: `{selected_count}`\n")
#               f.write("- Awaiting approval for deletion\n")
# 
#   # CHANGED: needs validate_deployments; condition uses validated_count
#   delete:
#     needs: [discover, validate_deployments, mark_for_delete, notify]
#     if: ${{ needs.validate_deployments.outputs.validated_count != '0' }}
#     runs-on: aks-runner-prd
#     environment: delete-approval
# 
#     steps:
#       # CHANGED (new): needed to read validated-repos.json in this job
#       - name: Download reports
#         uses: sndr-core/ra-workflows/actions/common/download-artifact@master
#         with:
#           Name: mark-reports
#           Path: reports/
# 
#       - name: Set up Python
#         uses: actions/setup-python@v6
#         with:
#           python-version: '3.11'
# 
#       - name: Rediscover and delete marked repositories
#         env:
#           GITHUB_TOKEN: ${{ secrets.GHEC_TOKEN }}
#           ORGANIZATIONS: ${{ needs.discover.outputs.organizations }}
#           AGE_DAYS: ${{ needs.discover.outputs.age_days }}
#         shell: bash
#         run: |
#           set -euo pipefail
#           mkdir -p reports
# 
#           python - <<'PY'
#           import csv
#           import json
#           import os
#           import re
#           import sys
#           from datetime import datetime, timezone
#           from urllib import error, request
# 
#           token = os.environ.get("GITHUB_TOKEN", "").strip()
#           organizations_raw = os.environ.get("ORGANIZATIONS", "sndr-core,sndr-secure")
#           age_days = int(os.environ.get("AGE_DAYS", "90"))
#           now = datetime.now(timezone.utc)
#           required_topic = "mark-for-delete"
# 
#           if not token:
#               print("Missing GHEC_TOKEN", file=sys.stderr)
#               sys.exit(1)
# 
#           organizations = [org.strip() for org in organizations_raw.split(",") if org.strip()]
#           if not organizations:
#               organizations = ["sndr-core", "sndr-secure"]
# 
#           # CHANGED (new): only repositories validated (and marked) in THIS run may
#           # be deleted - a repo that merely carries the mark-for-delete topic is not enough.
#           with open("reports/validated-repos.json", "r", encoding="utf-8") as f:
#               approved = {r["full_name"] for r in json.load(f)}
#           print(f"Validated repositories approved for deletion: {len(approved)}")
# 
#           def gh_request(url, method="GET"):
#               req = request.Request(url=url, method=method)
#               req.add_header("Authorization", f"Bearer {token}")
#               req.add_header("Accept", "application/vnd.github+json")
#               req.add_header("X-GitHub-Api-Version", "2022-11-28")
#               try:
#                   with request.urlopen(req, timeout=60) as resp:
#                       return resp.getcode(), resp.read().decode("utf-8")
#               except error.HTTPError as http_err:
#                   return http_err.code, http_err.read().decode("utf-8", errors="replace")
# 
#           # Rediscover archived repositories with mark-for-delete topic
#           candidates = []
#           for org in organizations:
#               page = 1
#               while True:
#                   url = f"https://api.github.com/orgs/{org}/repos?type=all&per_page=100&page={page}"
#                   status, body = gh_request(url)
#                   if status != 200:
#                       print(f"Failed listing repos for {org}, page {page}. HTTP {status}", file=sys.stderr)
#                       print(body, file=sys.stderr)
#                       sys.exit(1)
# 
#                   repos = json.loads(body)
#                   if not repos:
#                       break
# 
#                   for repo in repos:
#                       # Check if archived
#                       if not repo.get("archived", False):
#                           continue
# 
#                       # CHANGED (new): must be in this run's validated list
#                       if repo.get("full_name", "") not in approved:
#                           continue
# 
#                       # Check if has mark-for-delete topic
#                       topics = repo.get("topics", []) or []
#                       if required_topic not in topics:
#                           continue
# 
#                       # Check age threshold
#                       pushed_at = repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")
#                       if not pushed_at:
#                           continue
# 
#                       pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
#                       age = (now - pushed_dt).days
#                       if age <= age_days:
#                           continue
# 
#                       candidates.append({
#                           "full_name": repo.get("full_name", ""),
#                           "org": org,
#                           "repo": repo.get("name", ""),
#                           "age": age,
#                           "pushed_at": pushed_at
#                       })
# 
#                   page += 1
# 
#           print(f"Found {len(candidates)} repositories matching deletion criteria")
# 
#           if not candidates:
#               print("No repositories found with mark-for-delete topic and meeting age criteria.", file=sys.stderr)
#               with open("reports/delete-summary.md", "w", encoding="utf-8") as handle:
#                   handle.write("## Archived Repository Deletion Summary\n\n")
#                   handle.write("- No repositories found matching deletion criteria\n")
#                   handle.write(f"- Criteria: archived, has `{required_topic}` topic, age > {age_days} days\n")
#               sys.exit(0)
# 
#           # Delete repositories that match all criteria
#           results = []
#           deleted_count = 0
#           skipped_count = 0
#           failed_count = 0
# 
#           for item in candidates:
#               full_name = item["full_name"]
# 
#               delete_status, _ = gh_request(f"https://api.github.com/repos/{full_name}", method="DELETE")
#               if delete_status == 204:
#                   deleted_count += 1
#                   results.append({
#                       "full_name": full_name,
#                       "action": "deleted",
#                       "reason": f"archived-age-{item['age']}-topic-present",
#                       "http_status": 204
#                   })
#                   print(f"✓ Deleted: {full_name}")
#               else:
#                   failed_count += 1
#                   results.append({
#                       "full_name": full_name,
#                       "action": "failed",
#                       "reason": f"delete-http-{delete_status}",
#                       "http_status": delete_status
#                   })
#                   print(f"✗ Failed to delete: {full_name} (HTTP {delete_status})", file=sys.stderr)
# 
#           with open("reports/delete-results.csv", "w", encoding="utf-8", newline="") as handle:
#               writer = csv.DictWriter(handle, fieldnames=["full_name", "action", "reason", "http_status"])
#               writer.writeheader()
#               writer.writerows(results)
# 
#           with open("reports/delete-summary.md", "w", encoding="utf-8") as handle:
#               handle.write("## Archived Repository Deletion Summary\n\n")
#               handle.write(f"- Organizations scanned: `{', '.join(organizations)}`\n")
#               handle.write(f"- Age threshold (days): `{age_days}`\n")
#               handle.write(f"- Required topic: `{required_topic}`\n")
#               handle.write(f"- Repositories found: `{len(candidates)}`\n")
#               handle.write(f"- Deleted: `{deleted_count}`\n")
#               handle.write(f"- Failed: `{failed_count}`\n\n")
#               handle.write("| Repository | Action | Reason | HTTP |\n")
#               handle.write("|---|---|---|---:|\n")
#               for row in results:
#                   handle.write(f"| {row['full_name']} | {row['action']} | {row['reason']} | {row['http_status']} |\n")
# 
#           if failed_count > 0:
#               print(f"Deletion finished with {failed_count} failures.", file=sys.stderr)
#               sys.exit(1)
#           PY
# 
#       - name: Upload deletion reports
#         uses: actions/upload-artifact@v7
#         with:
#           name: deletion-reports
#           path: reports/
#           retention-days: 30
