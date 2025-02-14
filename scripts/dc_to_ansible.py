import contextlib
import json
import logging
import os
import re
import subprocess
from collections import defaultdict
from json import JSONDecodeError
from pathlib import Path
import typing as t
import click
import yaml

try:
    from dc import ComposeSpecification, Service, Volume
    from output_model import OutputModel
except ModuleNotFoundError:
    from .dc import ComposeSpecification, Service, Volume
    from .output_model import OutputModel

path_type = click.Path(path_type=Path, exists=True)
secret_provider_choices = click.Choice(("passwordstore",))
secret_provider_default = "passwordstore"
file_docker_file_mount_identifier: str = "# deploy-docker-compose-template::type"
file_type_env = f"{file_docker_file_mount_identifier}::env"


default_min_secret_len = 12


@contextlib.contextmanager
def cd_to_directory(path: Path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev_cwd)


def is_secret(
    service_name, key, value, secret_seqs: t.Sequence[str] | None = None
) -> bool:
    """Determines the type of variables by key name. Add additional logic using the service name of needed"""
    patt = re.compile("(password|secret|token|api_key|db_pass|secret_key)$", flags=re.IGNORECASE)
    if patt.search(key):
        return True
    elif secret_seqs and re.search(rf"({'|'.join(secret_seqs)})", key):
        return True
    return False


def patch_port(port_line: str, new_key):
    return re.sub(r"^\d+", new_key, port_line)

def validate_defaults_prefix(ctx, param, value):
    if re.search(r"[\s-]", value):
        raise click.BadParameter("Defaults prefix contains invalid characters")
    return value

@click.command()
@click.option(
    "--file",
    "-f",
    help="Docker compose file",
    type=path_type,
    default=Path("docker-compose.yml"),
)
@click.option(
    "--defaults-prefix", "-p", help="The prefix used for all defaults", prompt=True,
    type=click.UNPROCESSED, callback=validate_defaults_prefix
)
@click.option(
    "--secret-provider",
    type=secret_provider_choices,
    default=secret_provider_default,
    show_default=True,
)
@click.option(
    "--secret-string-template",
    help="Defines a string format for the path the secrets should be stored using defined secret provider.",
    default="services/{role_name}/{service_name}/{env_key}",
    show_default=True,
)
@click.option("--proxy-container", help="Container name to add to the proxy network")
@click.option("--role-name", "-n", default="docker_", show_default=True)
@click.option(
    "--min-secret-length", default=default_min_secret_len, type=int, show_default=True
)
@click.option(
    "--ext-proxy-net",
    "-e",
    help="Name of the external proxy net",
    default="proxy-tier",
    show_default=True,
)
@click.option(
    "--out", "-o", help="Output file", type=click.Path(path_type=Path), required=False
)
@click.option(
    "--uid", help="The users uid to use if user is defined on any service", type=int, required=False
)
def main(
    file,
    defaults_prefix,
    secret_provider,
    secret_string_template,
    proxy_container,
    role_name,
    min_secret_length,
    ext_proxy_net,
    out,
    uid
):
    """Converts a docker-compose file into an ansible role"""

    if not defaults_prefix.endswith("_"):
        defaults_prefix = f"{defaults_prefix}_"

    def normalize_key_and_name(name: str):
        return re.sub(r"[\s-]]", "_", name).lower()

    def variable_from_env(key):
        return f"{defaults_prefix}{normalize_key_and_name(key)}"

    def variable_from_port(
        service_name: str, exposed_port: int, add_prefix: bool = True
    ):
        return f"{defaults_prefix if add_prefix else ''}host_port_{service_name}_{exposed_port}"

    with cd_to_directory(file.parent):
        yaml_config = subprocess.check_output(
            (
                "docker",
                "compose",
                "-f",
                str(file.resolve()),
                "config",
                "--format",
                "json",
                "--no-path-resolution",
            ),
            encoding="utf-8",
        )
    try:
        yaml_data: dict = json.loads(yaml_config)
    except JSONDecodeError:
        # see bug https://github.com/docker/compose/issues/11669
        yaml_data = yaml.safe_load(yaml_config)

    model = ComposeSpecification.model_validate(yaml_data)
    final = yaml.safe_load(file.read_text(encoding="utf-8"))
    og_model = ComposeSpecification.model_validate(final)

    bootstrap_data: dict = {
        "role_name": normalize_key_and_name(role_name),
        "defaults": [],
        "ansible_vars": {},
        "example_defaults": {},
        "compose_vars_file": {},
        "secret_provider": secret_provider,
        "defaults_prefix": defaults_prefix,
        "secret_string_template": secret_string_template,
        "compose_files": [str(file.resolve())],
        "compose_config": yaml_data,
        "final_compose": {},
        "services_by_env": {},
        "proxy_container": proxy_container,
        "backup_paths": [],
        "example_playbook": [],
        "exposed_ports_by_service": defaultdict(list),
        "volume_defaults": {},
        "images_tags": [],
        "external_proxy_net": ext_proxy_net,
        "env_files": {},
    }

    services_by_env = defaultdict(list)
    volume_defaults = {}
    images_tags: dict[str:dict] = {}
    releases_key = normalize_key_and_name("releases")

    def get_secret_path(svc_name, key):
        context = dict(
            role_name=bootstrap_data["role_name"], service_name=svc_name, env_key=key
        )
        return secret_string_template.format(**context)

    def file_name_id(path: str):
        return normalize_key_and_name("_".join(Path(path).parts[-1].split(".")))

    def env_to_dict(env_: list | t.Iterable[str]) -> dict:
        e = {}
        for item in env_:
            k, v = item.split("=", 1)
            e[k] = v
        return e

    def env_from_file(p: Path, compose_env_file: bool = False) -> dict[str, str | int]:
        """Parses the env file and returns a dictionary of key value pairs
        For regular files: requires a line on top of the file
        For service.env_file files: set compose_env_file to True
        """
        def iterate():
            for ix, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
                if not compose_env_file and ix == 0 and not line.startswith(file_type_env):
                    # The user actually has to provide the file outside the logic of the role
                    break
                if not line or line.startswith("#"):
                    continue
                yield line

        return env_to_dict(iterate())

    def patch_image_tag(image: str, new_tag: str):
        splits = image.split(":")
        return ":".join((*splits[:-1], new_tag))

    def get_secret_expr(
        item: dict,
    ):
        """Returns the jinja variable expression using the secret path as defined by the secret_path template."""
        secret_len = max((len(item["value"]), min_secret_length))
        return f"{{{{ lookup('community.general.passwordstore', '{item['secret_path']} create=true length={secret_len}') }}}}"

    def add_to_defaults(
        key,
        val,
        service_name: str,
        is_secret: bool = False,
        env_file: str | None = None,
    ):
        if services_by_env.get(key):
            return
        normalized_key = variable_from_env(key)
        data = {
            "key": normalized_key,
            "original_key": key,
            "value": val,
            "is_secret": is_secret,
            "service": service_name,
        }
        if is_secret:
            data["secret_path"] = get_secret_path(service_name, key)
            data["secret_expr"] = get_secret_expr(data)
        if env_file:
            data["env_file"] = env_file
            bootstrap_data["env_files"][env_file] = file_name_id(env_file)
        bootstrap_data["defaults"].append(data)
        services_by_env[key].append(service_name)

    def handle_secret_env_var(svc_name: str, key, value, env_file: str | None = None):
        add_to_defaults(key, value, svc_name, is_secret=True, env_file=env_file)

    def handle_env_var(svc_name: str, key, value, env_file: str | None = None):
        add_to_defaults(key, value, svc_name, is_secret=False, env_file=env_file)

    def handle_svc_environment(name: str, svc: Service):
        env = svc.environment
        if not env:
            return
        env_file: str | list[str] = final["services"][name].get("env_file")
        names_to_env_files = {}
        if env_file:
            env_f_list =  [env_file] if isinstance(env_file, str) else env_file
            for ix, fp in enumerate(resolve_env_files(env_f_list)):
                file_env_data = env_from_file(fp, compose_env_file=True)
                names_to_env_files.update({k: env_f_list[ix] for k in file_env_data})
        # includes all env vars, also from env_file
        for key, value in env.root.items():
            if is_secret(name, key, value):
                handle_secret_env_var(name, key, value, env_file=names_to_env_files.get(key))
            else:
                handle_env_var(name, key, value, env_file=names_to_env_files.get(key))

    def handle_svc_env_file(name: str, path: str):
        """Takes every value scans for local files mounted as source into the container and extracts env vars from it.
        This is for the case where an env specifies the inner path to a config whereas ansible defaults contain these env vars
        in order to template this file to be mounted inside the docker container"""
        if path.startswith("/"):
            p = Path(path)
        else:
            p = file.parent / path
        p = p.resolve()
        if not p.is_file():
            return
        try:
            env = env_from_file(p)
        except Exception:
            logging.warning(f"Supposed env file {p} could not be parsed")
            return
        for key, value in env.items():
            if is_secret(name, key, value):
                handle_secret_env_var(name, key, value, path)
            else:
                handle_env_var(name, key, value, path)

    def handle_networks(networks_data: dict | None):
        if not networks_data:
            return

    def handle_svc_ports(name: str, data: Service):
        if not data.ports:
            return
        for port in data.ports:
            if not port.published:
                continue
            host_port = int(port.published)
            bootstrap_data["exposed_ports_by_service"][name].append(host_port)
            key = variable_from_port(name, host_port, add_prefix=False)
            add_to_defaults(key, host_port, name)

    def handle_svc_volumes(
        name: str, data: Service, volumes: dict[str, Volume | None] | None
    ):
        for vol in data.volumes or []:
            if vol.type == "bind":
                val = vol.source
                bootstrap_data["backup_paths"].append(val)
                handle_svc_env_file(name, path=val)
                m = re.search(r"[\w._]+$", val)
                vol_id = m.group(0) if m else "data"
                vol_id = vol_id.replace(".", "_")
                normalized_key = f"{defaults_prefix}{vol_id}_mount_dir"
                volume_defaults[val] = normalized_key

            elif vol.type == "volume":
                # may add the default path for docker volumes
                default_path = "/var/lib/docker/volumes"
                vol_path = volumes[vol.source].name
                bootstrap_data["backup_paths"].append(f"{default_path}/{vol_path}")

    supported = ("mariadb", "postgres", "redis")
    db_search_patt = re.compile(rf"({'|'.join(supported)})")

    def handle_service_image(name, svc: Service):
        image = svc.image
        m = db_search_patt.search(image)
        tag = image.split(":")[-1]
        data = {"service": name, "tag": tag}
        if m:
            data["kind"] = m.group(1)
        images_tags[image] = data

    def add_releases_to_defaults():
        defaults = {}
        for image, image_data in images_tags.items():
            defaults[image_data["service"]] = image_data["tag"]
        bootstrap_data["defaults"].append(
            {
                "key": variable_from_env(releases_key),
                "value": defaults,
                "is_secret": False,
                "service": "None",
            }
        )

    def handle_services(model: ComposeSpecification):
        for svc_name, svc in model.services.items():
            handle_svc_environment(svc_name, svc)
            handle_svc_volumes(svc_name, svc, model.volumes)
            handle_svc_ports(svc_name, svc)
            handle_service_image(svc_name, svc)

    def handle_proxy_container(compose_config: dict):
        if not proxy_container:
            return compose_config
        compose_config.setdefault("networks", {})
        compose_config["services"][proxy_container].setdefault("networks", [])
        compose_config["services"][proxy_container]["networks"].append(ext_proxy_net)
        if len(compose_config["services"][proxy_container]["networks"]) == 1:
            compose_config["services"][proxy_container]["networks"].append("default")
        compose_config["networks"].setdefault(
            ext_proxy_net, {"name": ext_proxy_net, "external": True}
        )
        return compose_config

    def resolve_env_files(file_list):
        for f in file_list:
            if f.startswith("/"):
                p = Path(f)
            else:
                p = file.parent / f
            yield p

    def create_final_compose(final_compose: dict):
        """Creates the final modified docker-compose.yml file with secret string
        and external networks added. Uses the original compose file before running
        `docker compose config` since this straps away the version of the files etc."""
        for name, data in final_compose["services"].items():
            env_ = data.get("environment", {})
            if isinstance(env_, list):
                env_ = env_to_dict(env_)
                final_compose["services"][name]["environment"] = env_
            for key, val in env_.items():
                lookup_var = variable_from_env(key)
                final_compose["services"][name]["environment"][key] = f"{{{{ {lookup_var} }}}}"
            for ix, vol in enumerate(data.get("volumes", [])):
                val = model.services[name].volumes[ix].source
                # val = vol["source"]
                if val in volume_defaults:
                    data["volumes"][ix] = data["volumes"][ix].replace(
                        val, f"{{{{ {volume_defaults[val]} }}}}"
                    )
            new_ports = []
            for port_entry, exposed_port in zip(
                data.get("ports", []),
                bootstrap_data["exposed_ports_by_service"].get(name, []),
            ):
                new_ports.append(
                    patch_port(
                        port_entry,
                        f"{{{{ {variable_from_port(name, exposed_port)} }}}}",
                    )
                )
            if new_ports:
                data["ports"] = new_ports
            img = data["image"]
            new_tag = f"{{{{ {variable_from_env(releases_key)}['{name}'] }}}}"
            data["image"] = patch_image_tag(img, new_tag)
            if uid and data.get("user"):
                data["user"] = re.sub(r"\d+", str(uid), data["user"])

        final_compose = handle_proxy_container(final)
        bootstrap_data["final_compose"] = final
        return final_compose

    handle_networks(model.networks)
    handle_services(model)
    add_releases_to_defaults()
    create_final_compose(final)
    bootstrap_data["services_by_env"] = services_by_env
    bootstrap_data["volume_defaults"] = volume_defaults
    bootstrap_data["images_tags"] = images_tags
    model = OutputModel.model_validate(bootstrap_data)

    def dump():
        return model.model_dump_json(indent=2, exclude_defaults=False)

    if out:
        out.write_text(dump(), encoding="utf-8")
    else:
        click.secho(dump())


if __name__ == "__main__":
    main()
