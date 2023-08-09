FROM python:3.11


RUN apt-get update \
    && apt-get install -y ssh gnupg software-properties-common curl gpg

RUN wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/hashicorp.list \
    && curl -L https://mirror.openshift.com/pub/openshift-v4/clients/rosa/latest/rosa-linux.tar.gz --output /tmp/rosa-linux.tar.gz \
    && tar xvf /tmp/rosa-linux.tar.gz --no-same-owner \
    && mv rosa /usr/bin/rosa \
    && chmod +x /usr/bin/rosa \
    && rosa version \
    && curl -L https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/stable/openshift-client-linux.tar.gz --output /tmp/openshift-client-linux.tar.gz \
    && tar xvf /tmp/openshift-client-linux.tar.gz --no-same-owner \
    && mv oc /usr/bin/oc \
    && mv kubectl /usr/bin/kubectl \
    && chmod +x /usr/bin/oc \
    && chmod +x /usr/bin/kubectl

RUN apt-get update \
    && apt-get install -y terraform


COPY pyproject.toml poetry.lock README.md /openshift-cli-installer/
COPY openshift_cli_installer /openshift-cli-installer/openshift_cli_installer/

WORKDIR /openshift-cli-installer
RUN mkdir clusters-install-data
RUN mkdir ssh-key
RUN ssh-keygen -t rsa -N '' -f /openshift-cli-installer/ssh-key/id_rsa


ENV POETRY_HOME=/openshift-cli-installer
ENV PATH="/openshift-cli-installer/bin:$PATH"

RUN python3 -m pip install pip --upgrade \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && poetry config cache-dir /openshift-cli-installer \
    && poetry config virtualenvs.in-project true \
    && poetry config installer.max-workers 10 \
    && poetry install



ENTRYPOINT ["poetry", "run", "python", "openshift_cli_installer/cli.py"]
