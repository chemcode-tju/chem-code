"""AiiDA transport using the same verified connection as the interactive research tools."""

from pathlib import Path

from aiida.transports.plugins.async_backend import _AsyncSSH
from aiida.transports.plugins.ssh_async import AsyncSshTransport

from chemistry_agent.automation.ssh_connection import connect
from chemistry_agent.automation.ssh_profiles import load_profile


class ProfileBackend(_AsyncSSH):
    # AiiDA 2.x provides the SFTP/exec implementation, but not a connection factory.
    # Override only the connection-opening hook; upstream close() owns the connection.
    async def open(self) -> None:
        connection = await connect(
            self.profile_root, load_profile(self.profile_root, self.identifier)
        )
        try:
            sftp = await connection.start_sftp_client()
        except BaseException:
            connection.close()
            await connection.wait_closed()
            raise
        self._conn = self._data_conn = connection
        self._sftp = sftp


class ProfileTransport(AsyncSshTransport):
    _valid_auth_options = [
        *AsyncSshTransport._valid_auth_options,
        ("profile_ref", {"type": str, "help": "Local Chem-Agent connection reference"}),
    ]

    def __init__(self, *args, profile_ref: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        path = Path(profile_ref)
        backend = ProfileBackend(self.machine, self.machine, self.logger, self._bash_command_str)
        backend.profile_root = path.parents[2]
        backend.identifier = path.stem
        self.async_backend = backend
