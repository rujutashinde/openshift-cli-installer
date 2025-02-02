import os
import shlex

import click
import yaml
from ocp_utilities.utils import run_command
from simple_logger.logger import get_logger

from openshift_cli_installer.libs.clusters.ocp_cluster import OCPCluster
from openshift_cli_installer.utils.cluster_versions import (
    filter_versions,
    get_aws_versions,
)
from openshift_cli_installer.utils.const import CREATE_STR, DESTROY_STR, PRODUCTION_STR
from openshift_cli_installer.utils.general import (
    generate_unified_pull_secret,
    get_install_config_j2_template,
    get_local_ssh_key,
    zip_and_upload_to_s3,
)


class AwsIpiCluster(OCPCluster):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"{self.__class__.__module__}-{self.__class__.__name__}")
        self.log_level = self.cluster.get("log_level", "error")

        if kwargs.get("destroy_from_s3_bucket_or_local_directory"):
            self._aws_download_installer()
        else:
            self.openshift_install_binary_path = None
            self.aws_base_available_versions = None
            self.cluster["ocm-env"] = self.cluster_info["ocm-env"] = PRODUCTION_STR

            self._prepare_aws_ipi_cluster()
            self.dump_cluster_data_to_file()

        self.prepare_cluster_data()

    def _prepare_aws_ipi_cluster(self):
        self.aws_base_available_versions = get_aws_versions()
        self.all_available_versions.update(
            filter_versions(
                wanted_version=self.cluster_info["user-requested-version"],
                base_versions_dict=self.aws_base_available_versions,
                platform=self.cluster_info["platform"],
                stream=self.cluster_info["stream"],
            )
        )
        self.set_cluster_install_version()
        self._set_install_version_url()
        self._aws_download_installer()
        if self.create:
            self._create_install_config_file()

    def _aws_download_installer(self):
        openshift_install_str = "openshift-install"
        version_url = self.cluster_info["version-url"]
        binary_dir = os.path.join("/tmp", version_url)
        self.openshift_install_binary_path = os.path.join(binary_dir, openshift_install_str)
        rc, _, err = run_command(
            command=shlex.split(
                "oc adm release extract "
                f"{version_url} "
                f"--command={openshift_install_str} --to={binary_dir} --registry-config={self.registry_config_file}"
            ),
            check=False,
        )
        if not rc:
            self.logger.error(
                f"{self.log_prefix}: Failed to get {openshift_install_str} for version {version_url}, error: {err}",
            )
            raise click.Abort()

    def _create_install_config_file(self):
        self.pull_secret = generate_unified_pull_secret(
            registry_config_file=self.registry_config_file,
            docker_config_file=self.docker_config_file,
        )
        self.ssh_key = get_local_ssh_key(ssh_key_file=self.ssh_key_file)

        terraform_parameters = {
            "name": self.cluster_info["name"],
            "region": self.cluster_info["region"],
            "base_domain": self.cluster_info["base-domain"],
            "platform": self.cluster_info["platform"],
            "ssh_key": self.ssh_key,
            "pull_secret": self.pull_secret,
        }

        worker_flavor = self.cluster.get("worker-flavor")
        if worker_flavor:
            terraform_parameters["worker_flavor"] = worker_flavor

        worker_root_disk_size = self.cluster.get("worker-root-disk-size")
        if worker_root_disk_size:
            terraform_parameters["worker_root_disk_size"] = worker_root_disk_size

        worker_replicas = self.cluster.get("worker-replicas")
        if worker_replicas:
            terraform_parameters["worker_replicas"] = worker_replicas

        fips = self.cluster.get("fips")
        if fips:
            terraform_parameters["fips"] = fips

        cluster_install_config = get_install_config_j2_template(jinja_dict=terraform_parameters)

        with open(os.path.join(self.cluster_info["cluster-dir"], "install-config.yaml"), "w") as fd:
            fd.write(yaml.dump(cluster_install_config))

    def _set_install_version_url(self):
        cluster_version = self.cluster["version"]
        version_url = [url for url, versions in self.aws_base_available_versions.items() if cluster_version in versions]
        if version_url:
            self.cluster_info["version-url"] = f"{version_url[0]}:{self.cluster_info['version']}"
        else:
            self.logger.error(
                f"{self.log_prefix}: Cluster version url not found for"
                f" {cluster_version} in {self.aws_base_available_versions.keys()}",
            )
            raise click.Abort()

    def run_installer_command(self, action, raise_on_failure):
        run_after_failed_create_str = (
            " after cluster creation failed" if action == DESTROY_STR and self.action == CREATE_STR else ""
        )
        self.logger.info(f"{self.log_prefix}: Running cluster {action}{run_after_failed_create_str}")
        res, out, err = run_command(
            command=shlex.split(
                f"{self.openshift_install_binary_path} {action} cluster --dir"
                f" {self.cluster_info['cluster-dir']} --log-level {self.log_level}"
            ),
            capture_output=False,
            check=False,
        )

        if not res:
            self.logger.error(
                f"{self.log_prefix}: Failed to run cluster {action} \n\tERR: {err}\n\tOUT: {out}.",
            )
            if raise_on_failure:
                raise click.Abort()

        return res, out, err

    def create_cluster(self):
        def _rollback_on_error(_ex=None):
            self.logger.error(f"{self.log_prefix}: Failed to create cluster: {_ex or 'No exception'}")
            if self.must_gather_output_dir:
                self.collect_must_gather()

            self.logger.warning(f"{self.log_prefix}: Cleaning cluster leftovers.")
            self.destroy_cluster()
            raise click.Abort()

        self.timeout_watch = self.start_time_watcher()
        res, _, _ = self.run_installer_command(action=CREATE_STR, raise_on_failure=False)

        if not res:
            _rollback_on_error()

        try:
            self.add_cluster_info_to_cluster_object()
            self.logger.success(f"{self.log_prefix}: Cluster created successfully")
            self.save_kubeadmin_token_to_clusters_install_data()

        except Exception as ex:
            _rollback_on_error(_ex=ex)

        if self.s3_bucket_name:
            zip_and_upload_to_s3(
                install_dir=self.cluster_info["cluster-dir"],
                s3_bucket_name=self.s3_bucket_name,
                s3_bucket_path=self.s3_bucket_path,
                uuid=self.cluster_info["shortuuid"],
            )

    def destroy_cluster(self):
        self.timeout_watch = self.start_time_watcher()
        self.run_installer_command(action=DESTROY_STR, raise_on_failure=True)
        self.logger.success(f"{self.log_prefix}: Cluster destroyed")
        self.delete_cluster_s3_buckets()
