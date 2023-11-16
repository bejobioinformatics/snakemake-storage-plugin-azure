from dataclasses import dataclass, field
import re
from urllib.parse import urlparse
from azure.storage.blob import BlobServiceClient, ContainerClient, BlobClient
from typing import Any, Iterable, Optional
from snakemake_interface_storage_plugins.settings import StorageProviderSettingsBase
from snakemake_interface_storage_plugins.storage_provider import (
    StorageProviderBase,
    StorageQueryValidationResult,
    ExampleQuery,
)
from snakemake_interface_storage_plugins.common import Operation
from snakemake_interface_storage_plugins.storage_object import (
    StorageObjectRead,
    StorageObjectWrite,
    StorageObjectGlob,
    retry_decorator,
)
from snakemake_interface_storage_plugins.io import IOCacheStorageInterface


def is_valid_azure_blob_endpoint(endpoint_url: str) -> bool:
    """
    Validates the Azure Blob endpoint pattern.

    Args:
    endpoint_url (str): The name of the Azure Blob Storage Account endpoint

    Returns:
    bool: True if the endpoint_url is a valid Azure Blob endpoint.
    """
    url_pattern = re.compile(
        r"^https:\/\/[a-z0-9]+(\.[a-z0-9]+)*\.blob\.core\.windows\.net\/?(.+)?$"
    )

    return bool(url_pattern.match(endpoint_url))


# Optional:
# Define settings for your storage plugin (e.g. host url, credentials).
# They will occur in the Snakemake CLI as --storage-<storage-plugin-name>-<param-name>
# Make sure that all defined fields are 'Optional' and specify a default value
# of None or anything else that makes sense in your case.
# Note that we allow storage plugin settings to be tagged by the user. That means,
# that each of them can be specified multiple times (an implicit nargs=+), and
# the user can add a tag in front of each value (e.g. tagname1:value1 tagname2:value2).
# This way, a storage plugin can be used multiple times within a workflow with different
# settings.
@dataclass
class StorageProviderSettings(StorageProviderSettingsBase):
    endpoint_url: Optional[str] = field(
        default=None,
        metadata={
            "help": "Azure Blob Storage Account endpoint url",
            # Optionally request that setting is also available for specification
            # via an environment variable. The variable will be named automatically as
            # SNAKEMAKE_<storage-plugin-name>_<param-name>, all upper case.
            # This mechanism should only be used for passwords, usernames, and other
            # credentials.
            # For other items, we rather recommend to let people use a profile
            # for setting defaults
            # (https://snakemake.readthedocs.io/en/stable/executing/cli.html#profiles).
            "env_var": False,
            # Optionally specify a function that parses the value given by the user.
            # This is useful to create complex types from the user input.
            "parse_func": ...,
            # If a parse_func is specified, you also have to specify an unparse_func
            # that converts the parsed value back to a string.
            "unparse_func": ...,
            # Optionally specify that setting is required when the executor is in use.
            "required": True,
        },
    )
    access_key: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Azure Blob Storage Account Access Key Credential.",
                "If set, takes precedence over sas_token credential.",
            ),
            "env_var": False,
        },
    )
    sas_token: Optional[str] = field(
        default=None,
        metadata={
            "help": "Azure Blob Storage Account SAS Token Credential",
            "env_var": False,
        },
    )

    def __post_init__(self):
        if not is_valid_azure_blob_endpoint(self.endpoint_url):
            raise ValueError(
                f"Invalid Azure Storage Blob Endpoint URL: {self.endpoint_url}"
            )

        self.credential = None
        if self.access_key:
            self.credential = self.access_key
        elif self.sas_token:
            self.credential = self.sas_token


# Required:
# Implementation of your storage provider
# This class can be empty as the one below.
# You can however use it to store global information or maintain e.g. a connection
# pool.
class StorageProvider(StorageProviderBase):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # further stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        self.bsc = BlobServiceClient(
            self.settings.endpoint_url, credential=self.settings.credential
        )

    def use_rate_limiter(self) -> bool:
        """Return False if no rate limiting is needed for this provider."""
        return False

    def default_max_requests_per_second(self) -> float:
        """Return the default maximum number of requests per second for this storage
        provider."""
        ...

    def rate_limiter_key(self, query: str, operation: Operation):
        """Return a key for identifying a rate limiter given a query and an operation.

        This is used to identify a rate limiter for the query.
        E.g. for a storage provider like http that would be the host name.
        For s3 it might be just the endpoint URL.
        """
        ...

    @classmethod
    def example_query(cls) -> ExampleQuery:
        """Return an example query with description for this storage provider."""
        return ExampleQuery(
            query="az://container/path/example/file.txt",
            description="A file in an Azure Blob Storage Container",
        )

    @classmethod
    def is_valid_query(cls, query: str) -> StorageQueryValidationResult:
        """Return whether the given query is valid for this storage provider."""
        # Ensure that also queries containing wildcards (e.g. {sample}) are accepted
        # and considered valid. The wildcards will be resolved before the storage
        # object is actually used.
        try:
            parsed = urlparse(query)
        except Exception as e:
            return StorageQueryValidationResult(
                query=query,
                valid=False,
                reason=f"cannot be parsed as URL ({e})",
            )
        if parsed.scheme != "az":
            return StorageQueryValidationResult(
                query=query,
                valid=False,
                reason="must start with az (az://...)",
            )
        return StorageQueryValidationResult(
            query=query,
            valid=True,
        )

    def list_objects(self, query: Any) -> Iterable[str]:
        """Return an iterator over all objects in the storage that match the query.

        This is optional and can raise a NotImplementedError() instead.
        """

        # parse container name from query
        parsed = urlparse(query)
        container_name = parsed.netloc
        cc = self.bsc.get_container_client(container_name)
        return [o for o in cc.list_blob_names()]


# Required:
# Implementation of storage object. If certain methods cannot be supported by your
# storage (e.g. because it is read-only see
# snakemake-storage-http for comparison), remove the corresponding base classes
# from the list of inherited items.
class StorageObject(StorageObjectRead, StorageObjectWrite, StorageObjectGlob):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # further stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        if self.is_valid_query():
            parsed = urlparse(self.query)
            self.container_name = parsed.netloc
            self.path = parsed.path.lstrip("/")

            # container client
            self.cc: ContainerClient = self.provider.bsc.get_container_client(
                self.container_name
            )

            # blob client
            self.bc: BlobClient = self.cc.get_blob_client(self.path)

    async def inventory(self, cache: IOCacheStorageInterface):
        """From this file, try to find as much existence and modification date
        information as possible. Only retrieve that information that comes for free
        given the current object.
        """
        # This is optional and can be left as is

        # If this is implemented in a storage object, results have to be stored in
        # the given IOCache object.
        pass

    def get_inventory_parent(self) -> Optional[str]:
        """Return the parent directory of this object."""
        # this is optional and can be left as is
        return self.cache_key(self.container_name)

    def local_suffix(self) -> str:
        """Return a unique suffix for the local path, determined from self.query."""
        return f"{self.container_name}/{self.path}"

    def cleanup(self):
        # Close any open connections, unmount stuff, etc.
        pass

    # Fallible methods should implement some retry logic.
    # The easiest way to do this (but not the only one) is to use the retry_decorator
    # provided by snakemake-interface-storage-plugins.
    @retry_decorator
    def exists(self) -> bool:
        # return True if the object exists
        return self.bc.exists()

    @retry_decorator
    def mtime(self) -> float:
        # return the modification time
        return self.bc.get_blob_properties().last_modified.timestamp()

    @retry_decorator
    def size(self) -> int:
        # return the size in bytes
        return self.bc.get_blob_properties().size

    @retry_decorator
    def retrieve_object(self):
        # Ensure that the object is accessible locally under self.local_path()
        ...

    # The following to methods are only required if the class inherits from
    # StorageObjectReadWrite.

    @retry_decorator
    def store_object(self):
        # Ensure that the object is stored at the location specified by
        # self.local_path().
        ...

    @retry_decorator
    def remove(self):
        # Remove the object from the storage.
        ...

    # The following to methods are only required if the class inherits from
    # StorageObjectGlob.

    @retry_decorator
    def list_candidate_matches(self) -> Iterable[str]:
        """Return a list of candidate matches in the storage for the query."""
        # This is used by glob_wildcards() to find matches for wildcards in the query.
        # The method has to return concretized queries without any remaining wildcards.
        ...

    def container_exists(self) -> bool:
        """Returns True if container exists, False otherwise."""
        try:
            container_name = urlparse(self.query).netloc
            return self.provider.bsc.get_container_client(container_name)
        except Exception:
            return False
