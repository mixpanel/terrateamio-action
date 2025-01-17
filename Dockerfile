FROM ghcr.io/terrateamio/action-base:latest

COPY conftest-wrapper /usr/local/bin/conftest-wrapper
COPY checkov-wrapper /usr/local/bin/checkov-wrapper
COPY cdktf-setup.sh /cdktf-setup.sh
COPY gcloud-cli-setup.sh /gcloud-cli-setup.sh
COPY entrypoint.sh /entrypoint.sh
COPY terrat_runner /terrat_runner

ENTRYPOINT ["/entrypoint.sh"]
