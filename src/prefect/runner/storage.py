import subprocess
from pathlib import Path
from typing import Dict, Optional, Protocol, TypedDict, Union, runtime_checkable
from urllib.parse import urlparse, urlunparse

from anyio import run_process

from prefect.blocks.core import Block
from prefect.blocks.system import Secret
from prefect.logging.loggers import get_logger


@runtime_checkable
class RunnerStorage(Protocol):
    """
    A storage interface for a runner to use to retrieve
    remotely stored flow code.
    """

    def set_base_path(self, path: Path):
        """
        Sets the base path to use when pulling contents from remote storage to
        local storage.
        """
        ...

    @property
    def pull_interval(self) -> Optional[int]:
        """
        The interval at which contents from remote storage should be pulled to
        local storage. If None, remote storage will perform a one-time sync.
        """
        ...

    @property
    def destination(self) -> Path:
        """
        The local file path to pull contents from remote storage to.
        """
        ...

    async def pull_code(self):
        """
        Pulls contents from remote storage to the local filesystem.
        """
        ...

    def to_pull_step(self) -> dict:
        """
        Returns a dictionary representation of the storage object that can be
        used as a deployment pull step.
        """
        ...

    def __eq__(self, __value) -> bool:
        """
        Equality check for runner storage objects.
        """
        ...


class GitCredentials(TypedDict, total=False):
    username: str
    access_token: Union[str, Secret]


class GitRepository:
    """
    Pulls the contents of a git repository to the local filesystem.

    Parameters:
        url: The URL of the git repository to pull from
        credentials: A dictionary of credentials to use when pulling from the
            repository. If a username is provided, an access token must also be
            provided.
        name: The name of the repository. If not provided, the name will be
            inferred from the repository URL.
        branch: The branch to pull from. Defaults to "main".
        pull_interval: The interval in seconds at which to pull contents from
            remote storage to local storage. If None, remote storage will perform
            a one-time sync.

    Examples:
        Pull the contents of a private git repository to the local filesystem:

        ```python
        from prefect.runner.storage import GitRepository

        storage = GitRepository(
            url="https://github.com/org/repo.git",
            credentials={"username": "oauth2", "access_token": "my-access-token"},
        )

        await storage.pull_code()
        ```
    """

    def __init__(
        self,
        url: str,
        credentials: Union[GitCredentials, Block, None] = None,
        name: Optional[str] = None,
        branch: Optional[str] = None,
        include_submodules: bool = False,
        pull_interval: Optional[int] = 60,
    ):
        if credentials is None:
            credentials = {}

        if (
            isinstance(credentials, dict)
            and credentials.get("username")
            and not credentials.get("access_token")
        ):
            raise ValueError(
                "If a username is provided, an access token must also be provided."
            )
        self._url = url
        self._branch = branch
        self._credentials = credentials
        self._include_submodules = include_submodules
        repo_name = urlparse(url).path.split("/")[-1].replace(".git", "")
        default_name = f"{repo_name}-{branch}" if branch else repo_name
        self._name = name or default_name
        self._logger = get_logger(f"runner.storage.git-repository.{self._name}")
        self._storage_base_path = Path.cwd()
        self._pull_interval = pull_interval

    @property
    def destination(self) -> Path:
        return self._storage_base_path / self._name

    def set_base_path(self, path: Path):
        self._storage_base_path = path

    @property
    def pull_interval(self) -> Optional[int]:
        return self._pull_interval

    @property
    def _repository_url_with_credentials(self) -> str:
        if not self._credentials:
            return self._url

        url_components = urlparse(self._url)

        credentials = (
            self._credentials.dict()
            if isinstance(self._credentials, Block)
            else self._credentials
        )
        if isinstance(credentials.get("access_token"), Secret):
            credentials["access_token"] = credentials["access_token"].get()

        formatted_credentials = _format_token_from_credentials(
            urlparse(self._url).netloc, credentials
        )
        if url_components.scheme == "https" and formatted_credentials is not None:
            updated_components = url_components._replace(
                netloc=f"{formatted_credentials}@{url_components.netloc}"
            )
            repository_url = urlunparse(updated_components)
        else:
            repository_url = self._url

        return repository_url

    async def pull_code(self):
        """
        Pulls the contents of the configured repository to the local filesystem.
        """
        self._logger.debug(
            "Pulling contents from repository '%s' to '%s'...",
            self._name,
            self.destination,
        )

        git_dir = self.destination / ".git"

        if git_dir.exists():
            # Check if the existing repository matches the configured repository
            result = await run_process(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=str(self.destination),
            )
            existing_repo_url = None
            if result.stdout is not None:
                existing_repo_url = _strip_auth_from_url(result.stdout.decode().strip())

            if existing_repo_url != self._url:
                raise ValueError(
                    f"The existing repository at {str(self.destination)} "
                    f"does not match the configured repository {self._url}"
                )

            self._logger.debug("Pulling latest changes from origin/%s", self._branch)
            # Update the existing repository
            cmd = ["git", "pull", "origin"]
            if self._branch:
                cmd += [self._branch]
            if self._include_submodules:
                cmd += ["--recurse-submodules"]
            cmd += ["--depth", "1"]
            await run_process(cmd, cwd=self.destination)
        else:
            self._logger.debug("Cloning repository %s", self._url)
            # Clone the repository if it doesn't exist at the destination

            repository_url = self._repository_url_with_credentials

            cmd = [
                "git",
                "clone",
                repository_url,
            ]
            if self._branch:
                cmd += ["--branch", self._branch]
            if self._include_submodules:
                cmd += ["--recurse-submodules"]

            # Limit git history and set path to clone to
            cmd += ["--depth", "1", str(self.destination)]

            try:
                await run_process(cmd)
            except subprocess.CalledProcessError as exc:
                # Hide the command used to avoid leaking the access token
                exc_chain = None if self._credentials else exc
                raise RuntimeError(
                    f"Failed to clone repository {self._url!r} with exit code"
                    f" {exc.returncode}."
                ) from exc_chain

    def __eq__(self, __value) -> bool:
        if isinstance(__value, GitRepository):
            return (
                self._url == __value._url
                and self._branch == __value._branch
                and self._name == __value._name
            )
        return False

    def __repr__(self) -> str:
        return (
            f"GitRepository(name={self._name!r} repository={self._url!r},"
            f" branch={self._branch!r})"
        )

    def to_pull_step(self) -> Dict:
        pull_step = {
            "prefect.deployments.steps.git_clone": {
                "repository": self._url,
                "branch": self._branch,
            }
        }
        if isinstance(self._credentials, Block):
            pull_step["prefect.deployments.steps.git_clone"][
                "credentials"
            ] = f"{{{{ {self._credentials.get_block_placeholder()} }}}}"
        elif isinstance(self._credentials, dict):
            if isinstance(self._credentials.get("access_token"), Secret):
                pull_step["prefect.deployments.steps.git_clone"]["credentials"] = {
                    **self._credentials,
                    "access_token": (
                        "{{"
                        f" {self._credentials['access_token'].get_block_placeholder()} }}}}"
                    ),
                }
            elif self._credentials.get("access_token") is not None:
                raise ValueError(
                    "Please save your access token as a Secret block before converting"
                    " this storage object to a pull step."
                )

        return pull_step


def create_storage_from_url(
    url: str, pull_interval: Optional[int] = 60
) -> RunnerStorage:
    """
    Creates a storage object from a URL.

    Args:
        url: The URL to create a storage object from
        pull_interval: The interval at which to pull contents from remote storage to
            local storage

    Returns:
        RunnerStorage: A runner storage compatible object
    """
    parsed_url = urlparse(url)
    if parsed_url.scheme == "git" or parsed_url.path.endswith(".git"):
        return GitRepository(url=url, pull_interval=pull_interval)
    raise ValueError(f"Unsupported storage URL: {url}. Only git URLs are supported.")


def _format_token_from_credentials(netloc: str, credentials: dict) -> str:
    """
    Formats the credentials block for the git provider.

    BitBucket supports the following syntax:
        git clone "https://x-token-auth:{token}@bitbucket.org/yourRepoOwnerHere/RepoNameHere"
        git clone https://username:<token>@bitbucketserver.com/scm/projectname/teamsinspace.git
    """
    username = credentials.get("username") if credentials else None
    password = credentials.get("password") if credentials else None
    token = credentials.get("token") if credentials else None
    access_token = credentials.get("access_token") if credentials else None

    user_provided_token = access_token or token or password

    if not user_provided_token:
        raise ValueError(
            "Please provide a `token` or `password` in your Credentials block to clone"
            " a repo."
        )

    if username:
        return f"{username}:{user_provided_token}"

    if "bitbucketserver" in netloc:
        # If they pass a BitBucketCredentials block and we don't have both a username and at
        # least one of a password or token and they don't provide a header themselves,
        # we can raise the appropriate error to avoid the wrong format for BitBucket Server.
        if not username and ":" not in user_provided_token:
            raise ValueError(
                "Please provide a `username` and a `password` or `token` in your"
                " BitBucketCredentials block to clone a repo from BitBucket Server."
            )
        # if username or if no username but it's provided in the token
        return (
            f"{username}:{user_provided_token}"
            if username and username not in user_provided_token
            else user_provided_token
        )

    elif "bitbucket" in netloc:
        return (
            user_provided_token
            if user_provided_token.startswith("x-token-auth:")
            or ":" in user_provided_token
            else f"x-token-auth:{user_provided_token}"
        )

    elif "gitlab" in netloc:
        return (
            f"oauth2:{user_provided_token}"
            if not user_provided_token.startswith("oauth2:")
            else user_provided_token
        )

    # all other cases (GitHub, etc.)
    return user_provided_token


def _strip_auth_from_url(url: str) -> str:
    parsed = urlparse(url)

    # Construct a new netloc without the auth info
    netloc = parsed.hostname
    if parsed.port:
        netloc += f":{parsed.port}"

    # Build the sanitized URL
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
