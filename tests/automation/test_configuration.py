from uuid import uuid4

import pytest
from pydantic import ValidationError

from chemistry_agent.automation.configuration import (
    PotentialSource,
)

POSCAR = "Cu\n1\n3.6 0 0\n0 3.6 0\n0 0 3.6\nCu\n1\nDirect\n0 0 0\n"












@pytest.mark.parametrize("root", ["/approved/../private", "/archive/PBE", "relative"])
def test_source_path_validation(root):
    with pytest.raises(ValidationError):
        PotentialSource(id="pbe", server_id=uuid4(), family="PBE", root=root)
