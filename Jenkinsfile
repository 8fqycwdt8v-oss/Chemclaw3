// Delivery for the ChemClaw3 core: build the one multi-role image, publish it, and roll it out.
//
// **This pipeline does not gate the change.** `.github/workflows/ci.yml` runs `make lint type cov`
// against a real Postgres plus the eleven validators, and `image.yml` proves the image builds and
// every component imports as a non-root UID. Reproducing that here would be a second answer to the
// same question, maintained by hand, in a second language. What Jenkins adds is the half GitHub
// Actions has never had: a registry to push to and a cluster to reach
// (`docs/planning/DEFERRED.md` — "Push-to-registry + `helm upgrade` rollout in CI").
//
// `RUN_GATE` exists for a Jenkins-only, air-gapped estate where the GitHub half does not run. It is
// off by default rather than absent, because a pipeline that silently gates nothing and a pipeline
// that deliberately gates elsewhere look identical from the outside.
//
// The four repositories publish independently and are rolled out together from one **release
// descriptor** — see `deploy/jenkins/README.md`. This pipeline produces this repository's row of it.
pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '30'))
    timeout(time: 90, unit: 'MINUTES')
  }

  parameters {
    string(name: 'IMAGE_REGISTRY', defaultValue: '',
           description: 'Registry and org, e.g. image-registry.openshift-image-registry.svc:5000/chemclaw. Empty = build and verify only, publish nothing.')
    string(name: 'IMAGE_NAME', defaultValue: 'chemclaw', description: 'Image name within the registry.')
    string(name: 'BASE_IMAGE', defaultValue: '',
           description: 'Optional base pinned by digest. deploy/Containerfile floats its default on purpose; a release should not (runbook §(xiv)).')
    choice(name: 'IMAGE_BUILDER', choices: ['autodetect', 'buildah', 'podman', 'kaniko', 'docker'],
           description: 'How to build. OpenShift agents get no Docker socket; buildah or kaniko is the usual answer there.')
    choice(name: 'DEPLOY_TARGET', choices: ['none', 'openshift', 'databricks'],
           description: 'Where to apply the release. Both targets read the same descriptor and each skips what the other owns.')
    choice(name: 'ENVIRONMENT', choices: ['dev', 'staging', 'prod'], description: 'Which environment values to apply.')
    string(name: 'NAMESPACE', defaultValue: '', description: 'Target namespace (openshift target).')
    booleanParam(name: 'DRY_RUN', defaultValue: true,
                 description: 'Render and diff without changing the cluster. Default true, deliberately.')
    booleanParam(name: 'RUN_GATE', defaultValue: false,
                 description: 'Run `make ci` here too. Off because GitHub Actions is the gate; on for a Jenkins-only estate.')
    booleanParam(name: 'ALLOW_ANY_EGRESS_DESTINATION', defaultValue: false,
                 description: 'State that any-destination egress is intended. The chart refuses to render without a stated posture.')
    string(name: 'REGISTRY_CREDENTIALS_ID', defaultValue: 'chemclaw-registry',
           description: 'Jenkins username/password credential for the registry.')
    string(name: 'CLUSTER_CREDENTIALS_ID', defaultValue: 'chemclaw-openshift',
           description: 'Jenkins secret-text credential holding the cluster API token (openshift target).')
    string(name: 'DATABRICKS_CREDENTIALS_ID', defaultValue: 'chemclaw-databricks',
           description: 'Jenkins secret-text credential holding a Databricks PAT (databricks target).')
    string(name: 'CLUSTER_API', defaultValue: '', description: 'Cluster API URL (openshift target).')
    string(name: 'DATABRICKS_HOST', defaultValue: '', description: 'Workspace URL (databricks target).')
  }

  environment {
    IMAGE_BUILDER = "${params.IMAGE_BUILDER == 'autodetect' ? '' : params.IMAGE_BUILDER}"
    VALUES_FILE = "deploy/jenkins/environments/${params.ENVIRONMENT}.yaml"
  }

  stages {
    stage('Preflight') {
      steps {
        script {
          env.REVISION = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          env.IMAGE_REF = params.IMAGE_REGISTRY ? "${params.IMAGE_REGISTRY}/${params.IMAGE_NAME}:${env.REVISION.take(12)}" : ''
          echo """revision   ${env.REVISION}
image      ${env.IMAGE_REF ?: '(not published)'}
target     ${params.DEPLOY_TARGET} / ${params.ENVIRONMENT}
dry run    ${params.DRY_RUN}"""
        }
        sh 'test -f deploy/Containerfile && test -d deploy/helm/chemclaw'
      }
    }

    // The full gate, only where nothing else runs it. `make cov`'s Postgres-backed tests skip
    // without a database and still print green — CLAUDE.md says never to report such a run as
    // green — so an agent running this needs `CHEMCLAW_POSTGRES_DSN` pointed at a real one.
    stage('Gate') {
      when { expression { params.RUN_GATE } }
      steps {
        sh 'uv sync --locked'
        sh 'make db-migrate'
        sh 'make ci'
      }
    }

    stage('Build and publish the image') {
      when { expression { params.IMAGE_REGISTRY != '' } }
      steps {
        withCredentials([usernamePassword(credentialsId: params.REGISTRY_CREDENTIALS_ID,
                                          usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_PASSWORD')]) {
          script {
            def buildArgs = "--build-arg CHEMCLAW_REVISION=${env.REVISION}"
            if (params.BASE_IMAGE) { buildArgs += " --build-arg BASE_IMAGE=${params.BASE_IMAGE}" }
            env.IMAGE_DIGEST = sh(returnStdout: true, script: """
              set -euo pipefail
              . deploy/jenkins/lib/registry-login.sh
              . deploy/jenkins/lib/image.sh
              registry_login '${params.IMAGE_REGISTRY}'
              build_and_push deploy/Containerfile . '${env.IMAGE_REF}' ${buildArgs}
            """).trim().readLines().last()
          }
        }
      }
    }

    // What only a built image can prove. The offline half — that the chart and the entrypoint agree
    // in both directions about which components exist — is `tests/test_deploy_chart.py`, so this
    // asserts the two things a file cannot: that the revision ARG reached the running process's
    // environment (`deployment_revision` is a column in the audit trail; it read "unknown" for
    // eight months), and that an unknown component exits 64 instead of quietly starting something.
    stage('Verify the image') {
      when { expression { params.IMAGE_REGISTRY != '' && params.IMAGE_BUILDER != 'kaniko' } }
      steps {
        sh '''
          set -euo pipefail
          runner="$(command -v podman || command -v docker)"
          revision="$("${runner}" run --rm --entrypoint sh "${IMAGE_REF}" -c 'printf %s "$CHEMCLAW_DEPLOYMENT_REVISION"')"
          test "${revision}" = "${REVISION}" || {
            echo "image reports revision '${revision}', expected '${REVISION}'" >&2; exit 1; }

          status=0
          "${runner}" run --rm --user 1001 -e CHEMCLAW_COMPONENT=not-a-component "${IMAGE_REF}" || status=$?
          test "${status}" -eq 64 || { echo "expected exit 64 for an unknown component, got ${status}" >&2; exit 1; }

          # Derived from the bundles present, so this cannot drift from what ships — the same
          # reasoning as `.github/workflows/image.yml`, which smoked a component that had not
          # existed for months while its hand-kept list still named it. Four of the eight bundles
          # have a server and no worker or the reverse, which is why each half is asked separately.
          for bundle in src/chemclaw/connectors/*/; do
            name="$(basename "${bundle}")"
            if [ -f "${bundle}/server/app.py" ]; then
              "${runner}" run --rm --user 1001 --entrypoint python "${IMAGE_REF}" \
                -c "import chemclaw.connectors.${name}.server.app"
            fi
            if [ -f "${bundle}/worker.py" ]; then
              "${runner}" run --rm --user 1001 --entrypoint python "${IMAGE_REF}" \
                -c "import chemclaw.connectors.${name}.worker"
            fi
          done
          echo "image verified"
        '''
      }
    }

    // The chart rendered against the Kubernetes schemas **with this release's digest and values**,
    // which is strictly more than `make helm-validate` does on the defaults. A broken chart is
    // otherwise discovered at `helm install`, in the namespace, on the worst day.
    stage('Render the chart') {
      when { expression { params.DEPLOY_TARGET == 'openshift' } }
      steps {
        script {
          // A digest is only available when this run published one; rendering with an empty
          // `image.digest` would exercise the tag path instead, which is not what gets deployed.
          def flags = env.IMAGE_DIGEST ? "--set image.digest=${env.IMAGE_DIGEST} --set image.repository=${params.IMAGE_REGISTRY}/${params.IMAGE_NAME}" : ''
          if (fileExists(env.VALUES_FILE)) { flags += " --values ${env.VALUES_FILE}" }
          if (params.ALLOW_ANY_EGRESS_DESTINATION) { flags += ' --set networkPolicy.allowAnyDestination=true' }
          sh """
            set -euo pipefail
            helm template chemclaw deploy/helm/chemclaw ${flags} > rendered.yaml
            kubeconform -strict -summary -ignore-missing-schemas rendered.yaml
          """
        }
      }
    }

    stage('Write the release descriptor') {
      steps {
        script {
          writeFile file: 'release.json', text: groovy.json.JsonOutput.prettyPrint(groovy.json.JsonOutput.toJson([
            environment: params.ENVIRONMENT,
            order: ['core'],
            components: [
              core: [
                kind: 'helm', release: 'chemclaw', chart: 'deploy/helm/chemclaw',
                repo: 'Chemclaw3', revision: env.REVISION,
                image: params.IMAGE_REGISTRY ? "${params.IMAGE_REGISTRY}/${params.IMAGE_NAME}" : '',
                digest: env.IMAGE_DIGEST ?: '',
                values: fileExists(env.VALUES_FILE) ? env.VALUES_FILE : '',
              ],
            ],
          ]))
          archiveArtifacts artifacts: 'release.json', fingerprint: true
        }
      }
    }

    stage('Deploy') {
      when { expression { params.DEPLOY_TARGET != 'none' } }
      steps {
        script {
          if (params.DEPLOY_TARGET == 'openshift') {
            withCredentials([string(credentialsId: params.CLUSTER_CREDENTIALS_ID, variable: 'CLUSTER_TOKEN')]) {
              sh """
                set -euo pipefail
                oc login --token="\${CLUSTER_TOKEN}" --server='${params.CLUSTER_API}' >/dev/null
                NAMESPACE='${params.NAMESPACE}' DRY_RUN='${params.DRY_RUN}' \
                ALLOW_ANY_EGRESS_DESTINATION='${params.ALLOW_ANY_EGRESS_DESTINATION}' \
                  deploy/jenkins/targets/openshift.sh release.json
              """
            }
          } else {
            withCredentials([string(credentialsId: params.DATABRICKS_CREDENTIALS_ID, variable: 'DATABRICKS_TOKEN')]) {
              sh """
                set -euo pipefail
                DATABRICKS_HOST='${params.DATABRICKS_HOST}' DRY_RUN='${params.DRY_RUN}' \
                  deploy/jenkins/targets/databricks.sh release.json
              """
            }
          }
        }
      }
    }

    // A rollout that reports success and a system that answers are different claims. `--wait`
    // proves the pods are ready; only this proves the front door serves — and `/readyz` is the one
    // that probes the connectors it dials, which is where a half-deployed fleet surfaces.
    stage('Smoke') {
      when { expression { params.DEPLOY_TARGET == 'openshift' && !params.DRY_RUN } }
      steps {
        sh """
          set -euo pipefail
          host="\$(oc get route chemclaw -n '${params.NAMESPACE}' -o jsonpath='{.spec.host}')"
          for probe in healthz readyz; do
            curl -fsS --max-time 20 "https://\${host}/\${probe}" >/dev/null \
              || { echo "the deployed front door failed /\${probe}" >&2; exit 1; }
          done
          echo "front door answers /healthz and /readyz"
        """
      }
    }
  }

  post {
    always { archiveArtifacts artifacts: 'rendered.yaml', allowEmptyArchive: true }
  }
}
