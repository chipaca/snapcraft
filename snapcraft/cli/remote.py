# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2019 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import click

from snapcraft.project import Project
from snapcraft.internal.remote_build import WorkTree, LaunchpadClient, errors
from snapcraft.formatting_utils import humanize_list
from typing import List
from xdg import BaseDirectory
from . import echo
from ._options import get_project, PromptOption

_SUPPORTED_ARCHS = ["amd64", "arm64", "armhf", "i386", "ppc64el", "s390x"]


@click.group()
def remotecli():
    """Remote build commands"""
    pass


@remotecli.command("remote-build")
@click.option(
    "--recover",
    metavar="<build-number>",
    type=int,
    nargs=1,
    required=False,
    help="Recover interrupted remote build.",
)
@click.option(
    "--status",
    metavar="<build-number>",
    type=int,
    nargs=1,
    required=False,
    help="Display remote build status.",
)
@click.option(
    "--arch",
    metavar="<arch-list>",
    type=str,
    nargs=1,
    required=False,
    help="Set architectures to build.",
)
@click.option(
    "--accept-public-upload",
    is_flag=True,
    prompt="All data sent to remote builders is public. Are you sure you want to continue?",
    help="Acknowledge that uploaded code is public.",
    cls=PromptOption,
)
@click.option(
    "--package-all-sources",
    is_flag=True,
    help="Package all sources to send to remote builder, not just local sources.",
)
@click.option(
    "--user",
    metavar="<username>",
    nargs=1,
    required=True,
    help="Launchpad username.",
    prompt="Launchpad username",
)
def remote_build(
    recover: int,
    status: int,
    user: str,
    arch: str,
    accept_public_upload: bool,
    package_all_sources: bool,
    echoer=echo,
) -> None:
    """Dispatch a snap for remote build.

    Command remote-build sends the current project to be built remotely. After the build
    is complete, packages for each architecture are retrieved and will be available in
    the local filesystem.

    If not specified in the snapcraft.yaml file, the list of architectures to build
    can be set using the --arch option. If both are specified, the architectures listed
    in snapcraft.yaml take precedence.

    Interrupted remote builds can be resumed using the --recover option, followed by
    the build number informed when the remote build was originally dispatched. The
    current state of the remote build for each architecture can be checked using the
    --status option.

    \b
    Examples:
        snapcraft remote-build --user <user>
        snapcraft remote-build --user <user> --arch=all
        snapcraft remote-build --user <user> --arch=amd64,armhf
        snapcraft remote-build --user <user> --recover 47860738
        snapcraft remote-build --user <user> --status 47860738
    """
    if not accept_public_upload:
        raise errors.AcceptPublicUploadError()

    echo.warning(
        "snapcraft remote-build is experimental and is subject to change - use with caution."
    )

    project = get_project()

    # TODO: use project.is_legacy() when available.
    base = project.info.get_build_base()
    if base is None:
        raise errors.BaseRequiredError()

    # Use a hash of current working directory to distinguish between other
    # potential project builds occurring in parallel elsewhere.
    project_hash = project._get_project_directory_hash()
    build_id = f"snapcraft-{project.info.name}-{project_hash}"

    # TODO: change login strategy after launchpad infrastructure is ready (LP #1827679)
    lp = LaunchpadClient(project=project, build_id=build_id, user=user)
    lp.login()

    if status:
        # Show build status
        lp.recover_build(status)
        for arch, build_status in lp.get_build_status():
            echo.info("{}: {}".format(arch, build_status))
    elif recover:
        # Recover from interrupted build
        echo.info("Recover build {}...".format(recover))
        lp.recover_build(recover)
        _monitor_build(lp)
    else:
        _start_build(
            lp=lp,
            project=project,
            arch=arch,
            build_id=build_id,
            package_all_sources=package_all_sources,
        )
        _monitor_build(lp)


def _start_build(
    *,
    lp: LaunchpadClient,
    project: Project,
    arch: str,
    build_id: str,
    package_all_sources: bool,
) -> None:
    # Pull/update sources for project.
    worktree_dir = BaseDirectory.save_data_path("snapcraft", "remote-build", build_id)
    wt = WorkTree(worktree_dir, project, package_all_sources=package_all_sources)
    repo_dir = wt.prepare_repository()
    lp.push_source_tree(repo_dir)

    # If build architectures not set in snapcraft.yaml, let the user override
    # Launchpad defaults using --arch.
    project_architectures = _get_project_architectures(project)
    if project_architectures and arch:
        raise click.BadOptionUsage(
            "Cannot use --arch, architecture list is already set in snapcraft.yaml."
        )
    archs = _choose_architectures(project_architectures, arch)

    # Sanity check for build architectures
    _check_supported_architectures(archs)

    # Create build recipe
    # (delete any existing snap to remove leftovers from previous builds)
    lp.delete_snap()
    lp.create_snap()

    targets = " for {}".format(humanize_list(archs, "and", "{}")) if archs else ""
    echo.info("Building package{}. This may take some time to finish.".format(targets))

    # Start building
    req_number = lp.start_build()
    echo.info(
        "If interrupted, resume with: 'snapcraft remote-build --recover {}'".format(
            req_number
        )
    )


def _monitor_build(lp: LaunchpadClient) -> None:
    lp.monitor_build()
    echo.info("Build complete.")
    lp.delete_snap()


def _check_supported_architectures(archs: List[str]) -> None:
    unsupported_archs = []
    for item in archs:
        if item not in _SUPPORTED_ARCHS:
            unsupported_archs.append(item)
    if unsupported_archs:
        raise errors.UnsupportedArchitectureError(archs=unsupported_archs)


def _choose_architectures(project_architectures: List[str], arch: str) -> List[str]:
    if project_architectures:
        archs = project_architectures
    elif arch == "all":
        archs = _SUPPORTED_ARCHS
    else:
        archs = arch.split(",") if arch else []

    return archs


def _get_project_architectures(project) -> List[str]:
    archs = []
    if project.info.architectures:
        for item in project.info.architectures:
            if "build-on" in item:
                new_arch = item["build-on"]
                if isinstance(new_arch, list):
                    # FIXME: LP builders don't recognize a list of architectures
                    archs.extend(new_arch)
                else:
                    archs.append(new_arch)
    return archs
