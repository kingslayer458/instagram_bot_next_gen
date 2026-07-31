pipeline {
    agent any

    triggers {
        cron('H */9 * * *')
    }

    stages {
        stage('Backup to GitHub') {
            steps {
                withCredentials([
                    sshUserPrivateKey(
                        credentialsId: 'digital_vm',
                        keyFileVariable: 'SSH_KEY',
                        usernameVariable: 'SSH_USER'
                    ),
                    string(
                        credentialsId: 'digital_SERVER_IP',
                        variable: 'digital_SERVER_IP'
                    )
                ]) {

                    sh '''
                    ssh -i "$SSH_KEY" \
                        -o StrictHostKeyChecking=no \
                        "$SSH_USER@$digital_SERVER_IP" << 'EOF'

                    set -e

                    cd /home/ubuntu/apps/instagram_bot_next_gen

                    echo "[+] Current Directory:"
                    pwd

                    echo "[+] Git Status:"
                    git status

                    echo "[+] Adding changes..."
                    git add .

                    if git diff --cached --quiet; then
                        echo "[+] No changes to commit."
                    else
                        echo "[+] Committing changes..."
                        git commit -m "Automated backup $(date '+%Y-%m-%d %H:%M:%S')"

                        echo "[+] Pushing to GitHub..."
                        git push origin HEAD
                    fi

                    echo "[+] Backup completed successfully."

EOF
                    '''
                }
            }
        }
    }

    post {
        success {
            echo 'Backup completed successfully.'
        }

        failure {
            echo 'Backup failed.'
        }
    }
}
