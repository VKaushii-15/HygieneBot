pipeline {
    agent any

    environment {
        AWS_DEFAULT_REGION = 'us-east-1'
        TF_IN_AUTOMATION   = 'true'
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    parameters {
        booleanParam(name: 'AUTO_APPLY', defaultValue: false, description: 'Automatically run terraform apply (skip manual approval)')
        booleanParam(name: 'DESTROY',    defaultValue: false, description: 'Tear down the infrastructure (terraform destroy)')
    }

    stages {

        // ── Checkout ────────────────────────────────────────────────
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        // ── Python Lint ─────────────────────────────────────────────
        stage('Lint Python') {
            steps {
                sh '''
                    echo "── Installing linting tools ──"
                    pip install --quiet flake8 pylint

                    echo "── Flake8 ──"
                    flake8 --max-line-length=120 --statistics \
                        scanner_lambda.py deleter_lambda.py \
                        src/lambda_scanner.py src/lambda_deletion.py || true

                    echo "── Pylint ──"
                    pylint --disable=C0114,C0115,C0116,E0401 --fail-under=6.0 \
                        scanner_lambda.py deleter_lambda.py \
                        src/lambda_scanner.py src/lambda_deletion.py || true
                '''
            }
        }

        // ── Terraform Validate ──────────────────────────────────────
        stage('Terraform Validate') {
            steps {
                sh '''
                    echo "── Terraform fmt check ──"
                    terraform fmt -check -diff -recursive .

                    echo "── Terraform init ──"
                    terraform init -backend=false -input=false

                    echo "── Terraform validate ──"
                    terraform validate
                '''
            }
        }

        // ── Package Lambdas ─────────────────────────────────────────
        stage('Package Lambdas') {
            steps {
                sh '''
                    echo "── Packaging scanner_lambda.zip ──"
                    zip -j scanner_lambda.zip scanner_lambda.py

                    echo "── Packaging deleter_lambda.zip ──"
                    zip -j deleter_lambda.zip deleter_lambda.py

                    echo "── Artifacts created ──"
                    ls -lh *.zip
                '''
            }
        }

        // ── Terraform Plan ──────────────────────────────────────────
        stage('Terraform Plan') {
            when {
                not { expression { return params.DESTROY } }
            }
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding',
                                  credentialsId: 'aws-credentials']]) {
                    sh '''
                        terraform init -input=false
                        terraform plan -out=tfplan -input=false
                    '''
                }
            }
        }

        // ── Manual Approval ─────────────────────────────────────────
        stage('Approval') {
            when {
                allOf {
                    branch 'main'
                    not { expression { return params.AUTO_APPLY } }
                    not { expression { return params.DESTROY } }
                }
            }
            steps {
                input message: 'Review the Terraform plan above. Proceed with apply?',
                      ok: 'Apply'
            }
        }

        // ── Terraform Apply ─────────────────────────────────────────
        stage('Terraform Apply') {
            when {
                allOf {
                    branch 'main'
                    not { expression { return params.DESTROY } }
                }
            }
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding',
                                  credentialsId: 'aws-credentials']]) {
                    sh 'terraform apply -auto-approve -input=false tfplan'
                }
            }
        }

        // ── Terraform Destroy (opt-in) ──────────────────────────────
        stage('Terraform Destroy') {
            when {
                expression { return params.DESTROY }
            }
            steps {
                input message: '⚠️  You are about to DESTROY all HygieneBot infrastructure. Continue?',
                      ok: 'Destroy'
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding',
                                  credentialsId: 'aws-credentials']]) {
                    sh '''
                        terraform init -input=false
                        terraform destroy -auto-approve -input=false
                    '''
                }
            }
        }
    }

    post {
        success {
            echo '✅ HygieneBot pipeline completed successfully.'
        }
        failure {
            echo '❌ HygieneBot pipeline failed. Check the logs above.'
        }
        always {
            cleanWs()
        }
    }
}
