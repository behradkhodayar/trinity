import logging
from typing import cast

from eth_typing import BLSSignature, Hash32
from eth_utils import ValidationError

from eth2._utils.bls import bls
from eth2._utils.humanize import humanize_bytes
from eth2.beacon.helpers import compute_domain, compute_signing_root
from eth2.beacon.types.attestations import Attestation
from eth2.beacon.types.blocks import BeaconBlock, SignedBeaconBlock
from eth2.beacon.typing import Operation, Root, SignedOperation
from eth2.validator_client.abc import BeaconNodeAPI, SignatoryDatabaseAPI
from eth2.validator_client.duty import Duty, DutyType
from eth2.validator_client.typing import PrivateKeyProvider

logger = logging.getLogger("eth2.validator_client.signatory")


async def _validate_duty(
    duty: Duty, operation: Operation, db: SignatoryDatabaseAPI
) -> None:
    """
    ``db`` contains a persistent record of all signatures with
    enough information to prevent the triggering of any slashing conditions.
    """
    if await db.is_slashable(duty, operation):
        raise ValidationError(
            f"signing the duty {duty} would result in a slashable signature"
        )


def sign(
    duty: Duty, operation: Operation, private_key_provider: PrivateKeyProvider
) -> BLSSignature:
    privkey = private_key_provider(duty.validator_public_key)
    # TODO use correct ``domain`` value
    # NOTE currently only uses part of the domain value
    # need to get fork from the state and compute the full domain value locally
    # NOTE: hardcoded for testing, based on generating the minimal set of validators
    genesis_validators_root = Root(
        Hash32(
            bytes.fromhex(
                "83431ec7fcf92cfc44947fc0418e831c25e1d0806590231c439830db7ad54fda"
            )
        )
    )
    domain = compute_domain(
        duty.signature_domain, genesis_validators_root=genesis_validators_root
    )
    signing_root = compute_signing_root(operation, domain)

    return bls.sign(privkey, signing_root)


def _attach_signature(
    duty: Duty, operation: Operation, signature: BLSSignature
) -> SignedOperation:
    if duty.duty_type == DutyType.Attestation:
        attestation = cast(Attestation, operation)
        return attestation.set("signature", signature)
    elif duty.duty_type == DutyType.BlockProposal:
        block_proposal = cast(BeaconBlock, operation)
        return SignedBeaconBlock.create(message=block_proposal, signature=signature)
    else:
        raise NotImplementedError(f"unrecognized duty type in duty {duty}")


async def sign_and_broadcast_operation_if_valid(
    duty: Duty,
    operation: Operation,
    signature_store: SignatoryDatabaseAPI,
    beacon_node: BeaconNodeAPI,
    private_key_provider: PrivateKeyProvider,
) -> None:
    try:
        await _validate_duty(duty, operation, signature_store)
    except ValidationError as e:
        logger.warning("a duty %s was not valid: %s", duty, e)
        return
    else:
        logger.debug(
            "received a valid duty %s for the operation with hash tree root %s; signing...",
            duty,
            humanize_bytes(operation.hash_tree_root),
        )

    await signature_store.record_signature_for(duty, operation)
    signature = sign(duty, operation, private_key_provider)

    operation_with_signature = _attach_signature(duty, operation, signature)

    logger.debug(
        "got signature %s for duty %s with (signed) hash tree root %s",
        humanize_bytes(signature),
        duty,
        humanize_bytes(operation_with_signature.hash_tree_root),
    )
    await beacon_node.publish(duty, operation_with_signature)
