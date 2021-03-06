from eth_utils import ValidationError
import pytest

from eth2.beacon.tools.fixtures.test_generation import (
    generate_pytests_from_eth2_fixture,
    pytest_from_eth2_fixture,
)
from eth2.beacon.tools.fixtures.test_types.bls import BLSTestType


def pytest_generate_tests(metafunc):
    generate_pytests_from_eth2_fixture(metafunc)


@pytest_from_eth2_fixture(
    {"test_types": {BLSTestType: lambda handler: handler.name == "aggregate"}}
)
def test_aggregate(test_case):
    if test_case.valid():
        test_case.execute()
    else:
        with pytest.raises(AssertionError):
            test_case.execute()


@pytest_from_eth2_fixture(
    {"test_types": {BLSTestType: lambda handler: handler.name == "sign"}}
)
def test_sign(test_case):
    test_case.execute()


@pytest_from_eth2_fixture(
    {"test_types": {BLSTestType: lambda handler: handler.name == "verify"}}
)
def test_verify(test_case):
    test_case.execute()


@pytest_from_eth2_fixture(
    {
        "test_types": {
            BLSTestType: lambda handler: handler.name == "fast_aggregate_verify"
        }
    }
)
def test_fast_aggregate_verify(test_case):
    if test_case.valid():
        test_case.execute()
    else:
        with pytest.raises(ValidationError):
            test_case.execute()


@pytest_from_eth2_fixture(
    {"test_types": {BLSTestType: lambda handler: handler.name == "aggregate_verify"}}
)
def test_aggregate_verify(test_case):
    if test_case.valid():
        test_case.execute()
    else:
        with pytest.raises(ValidationError):
            test_case.execute()
