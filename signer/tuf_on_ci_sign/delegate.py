# Copyright 2023 Google LLC

"""tuf-on-ci-delegate: A command line tool to modify TUF-on-CI delegations"""

import copy
import logging
import os
import re
from copy import deepcopy
from urllib import parse

import click
from securesystemslib.signer import (
    KEY_FOR_TYPE_AND_SCHEME,
    AzureSigner,
    GCPSigner,
    SigstoreKey,
    SSlibKey,
)

from tuf_on_ci_sign._common import (
    SignerConfig,
    bold,
    get_signing_key_input,
    git_echo,
    git_expect,
    signing_event,
)
from tuf_on_ci_sign._signer_repository import (
    OfflineConfig,
    OnlineConfig,
    SignerRepository,
    SignerState,
)

# sigstore is not a supported key by default
KEY_FOR_TYPE_AND_SCHEME[("sigstore-oidc", "Fulcio")] = SigstoreKey


logger = logging.getLogger(__name__)


def _get_offline_input(
    role: str,
    config: OfflineConfig,
) -> OfflineConfig:
    config = copy.deepcopy(config)
    click.echo(f"\nConfiguring role {role}")
    username_re = re.compile("^\\@[0-9a-zA-Z\\-]+$")

    def verify_signers(response: str) -> list[str]:
        # The list is presented in brackets [], if users tries to
        # respond with a list like expression, clear that.
        response = response.strip("[]")
        if not response:
            raise click.BadParameter("Must have at least one signer")

        signers: list[str] = []
        for s in response.split(","):
            s = s.strip()
            if not s.startswith("@"):
                s = f"@{s}"

            if not re.match(username_re, s):
                raise click.BadParameter(f"Invalid username {s}")
            signers.append(s)

        return signers

    while True:
        click.echo(
            f" 1. Configure signers: [{', '.join(config.signers)}], "
            f"requiring {config.threshold} signatures"
        )
        click.echo(
            f" 2. Configure expiry: Role expires in {config.expiry_period} days, "
            f"re-signing starts {config.signing_period} days before expiry"
        )
        choice = click.prompt(
            bold("Please choose an option or press enter to continue"),
            type=click.IntRange(0, 2),
            default=0,
            show_default=False,
        )
        if choice == 0:
            break
        if choice == 1:
            config.signers = click.prompt(
                bold(f"Please enter list of {role} signers"),
                default=", ".join(config.signers),
                value_proc=verify_signers,
            )

            if len(config.signers) == 1:
                config.threshold = 1
            else:
                config.threshold = click.prompt(
                    bold(f"Please enter {role} threshold"),
                    type=click.IntRange(1, len(config.signers)),
                    default=config.threshold,
                )

        elif choice == 2:
            config.expiry_period = click.prompt(
                bold(f"Please enter {role} expiry period in days"),
                type=int,
                default=config.expiry_period,
            )
            config.signing_period = click.prompt(
                bold(f"Please enter {role} signing period in days"),
                type=int,
                default=config.signing_period,
            )

    return config


def _get_repo_name(remote: str):
    url = parse.urlparse(git_expect(["config", "--get", f"remote.{remote}.url"]))
    repo = url.path[: -len(".git")]
    # ssh-urls are relative URLs according to urllib: host is actually part of
    # path. We don't want the host part:
    _, _, repo = repo.rpartition(":")
    # http urls on the other hand are not relative: remove the leading /
    return repo.lstrip("/")


def _sigstore_import(pull_remote: str) -> list[SigstoreKey]:
    # WORKAROUND: build sigstore key and uri here since there is no import yet
    issuer = "https://token.actions.githubusercontent.com"
    repo = _get_repo_name(pull_remote)

    # Create separate keys for the two workflows that need keys
    keys = []
    for workflow, keyid in [("snapshot.yml", "abcd"), ("version-bumps.yml", "efgh")]:
        id = f"https://github.com/{repo}/.github/workflows/{workflow}@refs/heads/main"
        key = SigstoreKey(
            keyid, "sigstore-oidc", "Fulcio", {"issuer": issuer, "identity": id}
        )
        key.unrecognized_fields["x-tuf-on-ci-online-uri"] = "sigstore:"
        keys.append(key)
    return keys


def _get_online_input(config: OnlineConfig, user_config: SignerConfig) -> OnlineConfig:
    config = copy.deepcopy(config)
    click.echo("\nConfiguring online roles")
    while True:
        keyuri = config.keys[0].unrecognized_fields["x-tuf-on-ci-online-uri"]
        click.echo(f" 1. Configure online key: {keyuri}")
        click.echo(
            f" 2. Configure timestamp: Expires in {config.timestamp_expiry} days,"
            f" re-signing starts {config.timestamp_signing} days before expiry"
        )
        click.echo(
            f" 3. Configure snapshot: Expires in {config.snapshot_expiry} days, "
            f"re-signing starts {config.snapshot_signing} days before expiry"
        )
        choice = click.prompt(
            bold("Please choose an option or press enter to continue"),
            type=click.IntRange(0, 3),
            default=0,
            show_default=False,
        )
        if choice == 0:
            break
        if choice == 1:
            config.keys = _collect_online_keys(user_config)
        if choice == 2:
            config.timestamp_expiry = click.prompt(
                bold("Please enter timestamp expiry in days"),
                type=int,
                default=config.timestamp_expiry,
            )
            config.timestamp_signing = click.prompt(
                bold("Please enter timestamp signing period in days"),
                type=int,
                default=config.timestamp_signing,
            )
        if choice == 3:
            config.snapshot_expiry = click.prompt(
                bold("Please enter snapshot expiry in days"),
                type=int,
                default=config.snapshot_expiry,
            )
            config.snapshot_signing = click.prompt(
                bold("Please enter snapshot signing period in days"),
                type=int,
                default=config.snapshot_signing,
            )

    return config


def _collect_online_keys(user_config: SignerConfig) -> list[SSlibKey]:
    # TODO use value_proc argument to validate the input

    while True:
        click.echo(" 1. Sigstore")
        click.echo(" 2. Google Cloud KMS")
        click.echo(" 3. Azure Key Vault")
        choice = click.prompt(
            bold("Please select online key type"),
            type=click.IntRange(1, 4),
            default=1,
            show_default=True,
        )
        if choice == 1:
            return _sigstore_import(user_config.pull_remote)
        if choice == 2:
            key_id = _collect_string("Enter a Google Cloud KMS key id")
            try:
                uri, key = GCPSigner.import_(key_id)
                key.unrecognized_fields["x-tuf-on-ci-online-uri"] = uri
                return [key]
            except Exception as e:
                raise click.ClickException(f"Failed to read Google Cloud KMS key: {e}")
        if choice == 3:
            vault_name = _collect_string("Enter Azure vault name")
            key_name = _collect_string("Enter key name")
            try:
                uri, key = AzureSigner.import_(vault_name, key_name)
                key.unrecognized_fields["x-tuf-on-ci-online-uri"] = uri
                return [key]
            except Exception as e:
                raise click.ClickException(f"Failed to read Azure Keyvault key: {e}")
        if choice == 4:
            # This could be generic support, but for now it's a hidden test key.
            # key value 1d9a024348e413892aeeb8cc8449309c152f48177200ee61a02ae56f450c6480
            uri = "envvar:LOCAL_TESTING_KEY"
            pub_key = "fa472895c9756c2b9bcd1440bf867d0fa5c4edee79e9792fa9822be3dd6fcbb3"
            key = SSlibKey(
                "fa47289",
                "ed25519",
                "ed25519",
                {"public": pub_key},
                {"x-tuf-on-ci-online-uri": uri},
            )
            return [key]


def _collect_string(prompt: str) -> str:
    while True:
        data = click.prompt(bold(prompt), default="")
        if data == "":
            continue
        else:
            return data


def _init_repository(repo: SignerRepository, user_config: SignerConfig) -> bool:
    click.echo("Creating a new TUF-on-CI repository")

    root_config = _get_offline_input(
        "root", OfflineConfig([repo.user_name], 1, 365, 60)
    )
    targets_config = _get_offline_input("targets", deepcopy(root_config))

    # As default we offer sigstore online key(s)
    keys = _sigstore_import(user_config.pull_remote)
    default_config = OnlineConfig(
        keys, 2, 1, root_config.expiry_period, root_config.signing_period
    )
    online_config = _get_online_input(default_config, user_config)

    key = None
    if (
        repo.user_name in root_config.signers
        or repo.user_name in targets_config.signers
    ):
        key = get_signing_key_input()

    repo.set_role_config("root", root_config, key)
    repo.set_role_config("targets", targets_config, key)
    repo.set_online_config(online_config)
    return True


def _update_online_roles(repo: SignerRepository, user_config: SignerConfig) -> bool:
    click.echo("Modifying online roles")

    config = repo.get_online_config()
    new_config = _get_online_input(config, user_config)
    if new_config == config:
        return False

    repo.set_online_config(new_config)
    return True


def _update_offline_role(repo: SignerRepository, role: str) -> bool:
    config = repo.get_role_config(role)
    if not config:
        # Non existent role
        click.echo(f"Creating a new delegation for {role}")
        new_config = _get_offline_input(
            role, OfflineConfig([repo.user_name], 1, 365, 60)
        )
    else:
        click.echo(f"Modifying delegation for {role}")
        new_config = _get_offline_input(role, config)
        if new_config == config:
            return False

    key = None
    if repo.user_name in new_config.signers:
        key = get_signing_key_input()

    repo.set_role_config(role, new_config, key)
    return True


@click.command()  # type: ignore[arg-type]
@click.option("-v", "--verbose", count=True, default=0)
@click.option("--push/--no-push", default=True)
@click.argument("event-name", metavar="SIGNING-EVENT")
@click.argument("role", required=False)
def delegate(verbose: int, push: bool, event_name: str, role: str | None):
    """Tool for modifying TUF-on-CI delegations."""
    logging.basicConfig(level=logging.WARNING - verbose * 10)

    toplevel = git_expect(["rev-parse", "--show-toplevel"])
    settings_path = os.path.join(toplevel, ".tuf-on-ci-sign.ini")
    user_config = SignerConfig(settings_path)

    with signing_event(event_name, user_config) as repo:
        if repo.state == SignerState.UNINITIALIZED:
            changed = _init_repository(repo, user_config)
        else:
            if role is None:
                role = click.prompt(bold("Enter name of role to modify"))

            if role in ["timestamp", "snapshot"]:
                changed = _update_online_roles(repo, user_config)
            else:
                changed = _update_offline_role(repo, role)

        if changed:
            if role:
                msg = f"'{role}' role/delegation change"
            else:
                msg = "Initial root and targets"
            git_expect(["add", "metadata/"])
            git_expect(["commit", "-m", msg, "--", "metadata"])

            if repo.unsigned:
                click.echo(f"Your signature is required for role(s) {repo.unsigned}.")

                for rolename in repo.unsigned:
                    click.echo(repo.status(rolename))
                    repo.sign(rolename)

                git_expect(["add", "metadata/"])
                git_expect(["commit", "-m", f"Signed by {user_config.user_name}"])

            if push:
                branch = f"{user_config.push_remote}/{event_name}"
                msg = f"Press enter to push changes to {branch}"
                click.prompt(bold(msg), default=True, show_default=False)
                git_echo(
                    [
                        "push",
                        "--progress",
                        user_config.push_remote,
                        f"HEAD:refs/heads/{event_name}",
                    ]
                )
            else:
                # TODO: deal with existing branch?
                click.echo(f"Creating local branch {event_name}")
                git_expect(["branch", event_name])
        else:
            click.echo("Nothing to do")
