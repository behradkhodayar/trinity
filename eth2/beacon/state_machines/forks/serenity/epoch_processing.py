from typing import Sequence, Set, Tuple

from eth_typing import Hash32
from eth_utils.toolz import curry
from ssz.hashable_list import HashableList

from eth2._utils.tuple import update_tuple_item, update_tuple_item_with_fn
from eth2.beacon.constants import BASE_REWARDS_PER_EPOCH, GENESIS_EPOCH
from eth2.beacon.epoch_processing_helpers import (
    compute_activation_exit_epoch,
    decrease_balance,
    get_attesting_balance,
    get_attesting_indices,
    get_base_reward,
    get_matching_head_attestations,
    get_matching_source_attestations,
    get_matching_target_attestations,
    get_total_active_balance,
    get_total_balance,
    get_unslashed_attesting_indices,
    get_validator_churn_limit,
)
from eth2.beacon.helpers import get_block_root, get_randao_mix
from eth2.beacon.types.checkpoints import Checkpoint
from eth2.beacon.types.eth1_data import Eth1Data
from eth2.beacon.types.historical_batch import HistoricalBatch
from eth2.beacon.types.pending_attestations import PendingAttestation
from eth2.beacon.types.states import BeaconState
from eth2.beacon.types.validators import Validator
from eth2.beacon.typing import Bitfield, Epoch, Gwei, ValidatorIndex
from eth2.beacon.validator_status_helpers import initiate_exit_for_validator
from eth2.configs import Eth2Config


def _bft_threshold_met(participation: Gwei, total: Gwei) -> bool:
    return 3 * participation >= 2 * total


def _is_threshold_met_against_active_set(
    state: BeaconState, attestations: Sequence[PendingAttestation], config: Eth2Config
) -> bool:
    """
    Predicate indicating if the balance at risk of validators making an attestation
    in ``attestations`` is greater than the fault tolerance threshold of the total balance.
    """
    attesting_balance = get_attesting_balance(state, attestations, config)

    total_balance = get_total_active_balance(state, config)

    return _bft_threshold_met(attesting_balance, total_balance)


def _is_epoch_justifiable(state: BeaconState, epoch: Epoch, config: Eth2Config) -> bool:
    attestations = get_matching_target_attestations(state, epoch, config)
    return _is_threshold_met_against_active_set(state, attestations, config)


def _determine_updated_justification_data(
    justified_epoch: Epoch,
    bitfield: Bitfield,
    is_epoch_justifiable: bool,
    candidate_epoch: Epoch,
    bit_offset: int,
) -> Tuple[Epoch, Bitfield]:
    if is_epoch_justifiable:
        return (
            candidate_epoch,
            Bitfield(update_tuple_item(bitfield, bit_offset, True)),
        )
    else:
        return (justified_epoch, bitfield)


def _determine_updated_justifications(
    previous_epoch_justifiable: bool,
    previous_epoch: Epoch,
    current_epoch_justifiable: bool,
    current_epoch: Epoch,
    justified_epoch: Epoch,
    justification_bits: Bitfield,
) -> Tuple[Epoch, Bitfield]:
    (justified_epoch, justification_bits) = _determine_updated_justification_data(
        justified_epoch,
        justification_bits,
        previous_epoch_justifiable,
        previous_epoch,
        1,
    )

    (justified_epoch, justification_bits) = _determine_updated_justification_data(
        justified_epoch, justification_bits, current_epoch_justifiable, current_epoch, 0
    )

    return (justified_epoch, justification_bits)


def _determine_new_justified_epoch_and_bitfield(
    state: BeaconState, config: Eth2Config
) -> Tuple[Epoch, Bitfield]:
    previous_epoch = state.previous_epoch(config.SLOTS_PER_EPOCH)
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)

    previous_epoch_justifiable = _is_epoch_justifiable(state, previous_epoch, config)
    current_epoch_justifiable = _is_epoch_justifiable(state, current_epoch, config)

    (
        new_current_justified_epoch,
        justification_bits,
    ) = _determine_updated_justifications(
        previous_epoch_justifiable,
        previous_epoch,
        current_epoch_justifiable,
        current_epoch,
        state.current_justified_checkpoint.epoch,
        (False,) + state.justification_bits[:-1],
    )

    return (new_current_justified_epoch, justification_bits)


def _determine_new_justified_checkpoint_and_bitfield(
    state: BeaconState, config: Eth2Config
) -> Tuple[Checkpoint, Bitfield]:
    (
        new_current_justified_epoch,
        justification_bits,
    ) = _determine_new_justified_epoch_and_bitfield(state, config)

    if new_current_justified_epoch != state.current_justified_checkpoint.epoch:
        new_current_justified_root = get_block_root(
            state,
            new_current_justified_epoch,
            config.SLOTS_PER_EPOCH,
            config.SLOTS_PER_HISTORICAL_ROOT,
        )
    else:
        new_current_justified_root = state.current_justified_checkpoint.root

    return (
        Checkpoint.create(
            epoch=new_current_justified_epoch, root=new_current_justified_root
        ),
        justification_bits,
    )


def _bitfield_matches(bitfield: Bitfield, offset: slice) -> bool:
    return all(bitfield[offset])


def _determine_new_finalized_epoch(
    last_finalized_epoch: Epoch,
    previous_justified_epoch: Epoch,
    current_justified_epoch: Epoch,
    current_epoch: Epoch,
    justification_bits: Bitfield,
) -> Epoch:
    new_finalized_epoch = last_finalized_epoch

    if (
        _bitfield_matches(justification_bits, slice(1, 4))
        and previous_justified_epoch + 3 == current_epoch
    ):
        new_finalized_epoch = previous_justified_epoch

    if (
        _bitfield_matches(justification_bits, slice(1, 3))
        and previous_justified_epoch + 2 == current_epoch
    ):
        new_finalized_epoch = previous_justified_epoch

    if (
        _bitfield_matches(justification_bits, slice(0, 3))
        and current_justified_epoch + 2 == current_epoch
    ):
        new_finalized_epoch = current_justified_epoch

    if (
        _bitfield_matches(justification_bits, slice(0, 2))
        and current_justified_epoch + 1 == current_epoch
    ):
        new_finalized_epoch = current_justified_epoch

    return new_finalized_epoch


def _determine_new_finalized_checkpoint(
    state: BeaconState, justification_bits: Bitfield, config: Eth2Config
) -> Checkpoint:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)

    new_finalized_epoch = _determine_new_finalized_epoch(
        state.finalized_checkpoint.epoch,
        state.previous_justified_checkpoint.epoch,
        state.current_justified_checkpoint.epoch,
        current_epoch,
        justification_bits,
    )
    if new_finalized_epoch != state.finalized_checkpoint.epoch:
        # NOTE: we only want to call ``get_block_root``
        # upon some change, not unconditionally
        # Given the way it reads the block roots, it can cause
        # validation problems with some configurations, esp. in testing.
        # This is implicitly happening above for the justified roots.
        new_finalized_root = get_block_root(
            state,
            new_finalized_epoch,
            config.SLOTS_PER_EPOCH,
            config.SLOTS_PER_HISTORICAL_ROOT,
        )
    else:
        new_finalized_root = state.finalized_checkpoint.root

    return Checkpoint.create(epoch=new_finalized_epoch, root=new_finalized_root)


def process_justification_and_finalization(
    state: BeaconState, config: Eth2Config
) -> BeaconState:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)

    if current_epoch <= GENESIS_EPOCH + 1:
        return state

    (
        new_current_justified_checkpoint,
        justification_bits,
    ) = _determine_new_justified_checkpoint_and_bitfield(state, config)

    new_finalized_checkpoint = _determine_new_finalized_checkpoint(
        state, justification_bits, config
    )

    return state.mset(
        "justification_bits",
        justification_bits,
        "previous_justified_checkpoint",
        state.current_justified_checkpoint,
        "current_justified_checkpoint",
        new_current_justified_checkpoint,
        "finalized_checkpoint",
        new_finalized_checkpoint,
    )


def _is_threshold_met_against_committee(
    state: BeaconState,
    attesting_indices: Set[ValidatorIndex],
    committee: Set[ValidatorIndex],
    config: Eth2Config,
) -> bool:
    total_attesting_balance = get_total_balance(state, attesting_indices, config)
    total_committee_balance = get_total_balance(state, committee, config)
    return _bft_threshold_met(total_attesting_balance, total_committee_balance)


def get_proposer_reward(
    state: BeaconState, attesting_index: ValidatorIndex, config: Eth2Config
) -> Gwei:
    return Gwei(
        get_base_reward(state, attesting_index, config)
        // config.PROPOSER_REWARD_QUOTIENT
    )


def get_finality_delay(state: BeaconState, slots_per_epoch: int) -> int:
    return state.previous_epoch(slots_per_epoch) - state.finalized_checkpoint.epoch


def is_in_inactivity_leak(state: BeaconState, config: Eth2Config) -> bool:
    return (
        get_finality_delay(state, config.SLOTS_PER_EPOCH)
        > config.MIN_EPOCHS_TO_INACTIVITY_PENALTY
    )


def get_attestation_deltas(
    state: BeaconState, config: Eth2Config
) -> Tuple[Sequence[Gwei], Sequence[Gwei]]:
    rewards = tuple(0 for _ in range(len(state.validators)))
    penalties = tuple(0 for _ in range(len(state.validators)))
    previous_epoch = state.previous_epoch(config.SLOTS_PER_EPOCH)
    total_balance = get_total_active_balance(state, config)
    eligible_validator_indices = tuple(
        ValidatorIndex(index)
        for index, v in enumerate(state.validators)
        if v.is_active(previous_epoch)
        or (v.slashed and previous_epoch + 1 < v.withdrawable_epoch)
    )

    matching_source_attestations = get_matching_source_attestations(
        state, previous_epoch, config
    )
    matching_target_attestations = get_matching_target_attestations(
        state, previous_epoch, config
    )
    matching_head_attestations = get_matching_head_attestations(
        state, previous_epoch, config
    )

    increment = config.EFFECTIVE_BALANCE_INCREMENT
    total_balance_in_increment = total_balance // increment
    for attestations in (
        matching_source_attestations,
        matching_target_attestations,
        matching_head_attestations,
    ):
        unslashed_attesting_indices = get_unslashed_attesting_indices(
            state, attestations, config
        )
        attesting_balance = get_total_balance(
            state, unslashed_attesting_indices, config
        )
        attesting_balance_in_increment = attesting_balance // increment
        for index in eligible_validator_indices:
            if index in unslashed_attesting_indices:
                if is_in_inactivity_leak(state, config):
                    reward = get_base_reward(state, index, config)
                else:
                    reward = Gwei(
                        (
                            get_base_reward(state, index, config)
                            * attesting_balance_in_increment
                        )
                        // total_balance_in_increment
                    )
                rewards = update_tuple_item_with_fn(
                    rewards, index, lambda balance, delta: balance + delta, reward
                )
            else:
                penalties = update_tuple_item_with_fn(
                    penalties,
                    index,
                    lambda balance, delta: balance + delta,
                    get_base_reward(state, index, config),
                )

    for index in get_unslashed_attesting_indices(
        state, matching_source_attestations, config
    ):
        attestation = min(
            (
                a
                for a in matching_source_attestations
                if index
                in get_attesting_indices(state, a.data, a.aggregation_bits, config)
            ),
            key=lambda a: a.inclusion_delay,
        )
        proposer_reward = get_proposer_reward(state, index, config)
        rewards = update_tuple_item_with_fn(
            rewards,
            attestation.proposer_index,
            lambda balance, delta: balance + delta,
            proposer_reward,
        )
        base_reward = get_base_reward(state, index, config)
        max_attester_reward = base_reward - proposer_reward
        rewards = update_tuple_item_with_fn(
            rewards,
            index,
            lambda balance, delta: balance + delta,
            (max_attester_reward // attestation.inclusion_delay),
        )

    if is_in_inactivity_leak(state, config):
        matching_target_attesting_indices = get_unslashed_attesting_indices(
            state, matching_target_attestations, config
        )
        for index in eligible_validator_indices:
            base_reward = get_base_reward(state, index, config)
            penalties = update_tuple_item_with_fn(
                penalties,
                index,
                lambda balance, delta: balance + delta,
                BASE_REWARDS_PER_EPOCH * base_reward
                - get_proposer_reward(state, index, config),
            )
            if index not in matching_target_attesting_indices:
                effective_balance = state.validators[index].effective_balance
                penalties = update_tuple_item_with_fn(
                    penalties,
                    index,
                    lambda balance, delta: balance + delta,
                    effective_balance
                    * get_finality_delay(state, config.SLOTS_PER_EPOCH)
                    // config.INACTIVITY_PENALTY_QUOTIENT,
                )
    return (
        tuple(Gwei(reward) for reward in rewards),
        tuple(Gwei(penalty) for penalty in penalties),
    )


def process_rewards_and_penalties(
    state: BeaconState, config: Eth2Config
) -> BeaconState:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)
    if current_epoch == GENESIS_EPOCH:
        return state

    rewards_for_attestations, penalties_for_attestations = get_attestation_deltas(
        state, config
    )

    new_balances = (
        max(balance + reward - penalty, 0)
        for balance, reward, penalty in zip(
            state.balances, rewards_for_attestations, penalties_for_attestations
        )
    )

    return state.set(
        "balances", HashableList.from_iterable(new_balances, state.balances.sedes)
    )


@curry
def _process_activation_eligibility_or_ejections(
    state: BeaconState, index: ValidatorIndex, config: Eth2Config
) -> BeaconState:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)
    validator = state.validators[index]

    if validator.is_eligible_for_activation_queue(config):
        validator = validator.set("activation_eligibility_epoch", current_epoch + 1)

    if (
        validator.is_active(current_epoch)
        and validator.effective_balance <= config.EJECTION_BALANCE
    ):
        validator = initiate_exit_for_validator(validator, state, config)

    return state.transform(("validators", index), validator)


@curry
def _update_validator_activation_epoch(
    state: BeaconState, config: Eth2Config, validator: Validator
) -> Validator:
    return validator.set(
        "activation_epoch",
        compute_activation_exit_epoch(
            state.current_epoch(config.SLOTS_PER_EPOCH), config.MAX_SEED_LOOKAHEAD
        ),
    )


def process_registry_updates(state: BeaconState, config: Eth2Config) -> BeaconState:
    new_state = state
    for index in range(len(state.validators)):
        new_state = _process_activation_eligibility_or_ejections(
            new_state, index, config
        )

    # Queue validators eligible for activation and not yet dequeued for activation
    activation_queue = sorted(
        (
            index
            for index, validator in enumerate(new_state.validators)
            if validator.is_eligible_for_activation(state)
        ),
        # Order by the sequence of activation_eligibility_epoch setting and then index
        key=lambda index: (
            new_state.validators[index].activation_eligibility_epoch,
            index,
        ),
    )

    # Dequeued validators for activation up to churn limit
    for index in activation_queue[: get_validator_churn_limit(state, config)]:
        new_state = new_state.transform(
            ("validators", index), _update_validator_activation_epoch(state, config)
        )

    return new_state


def _determine_slashing_penalty(
    total_penalties: Gwei, total_balance: Gwei, balance: Gwei, increment: Gwei
) -> Gwei:
    penalty_numerator = balance // increment * min(total_penalties * 3, total_balance)
    penalty = penalty_numerator // total_balance * increment
    return Gwei(penalty)


def process_slashings(state: BeaconState, config: Eth2Config) -> BeaconState:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)
    total_balance = get_total_active_balance(state, config)

    slashing_period = config.EPOCHS_PER_SLASHINGS_VECTOR // 2
    for index, validator in enumerate(state.validators):
        index = ValidatorIndex(index)
        if (
            validator.slashed
            and current_epoch + slashing_period == validator.withdrawable_epoch
        ):
            penalty = _determine_slashing_penalty(
                Gwei(sum(state.slashings)),
                total_balance,
                validator.effective_balance,
                config.EFFECTIVE_BALANCE_INCREMENT,
            )
            state = decrease_balance(state, index, penalty)
    return state


def _determine_next_eth1_votes(
    state: BeaconState, config: Eth2Config
) -> HashableList[Eth1Data]:
    if (
        state.next_epoch(config.SLOTS_PER_EPOCH) % config.EPOCHS_PER_ETH1_VOTING_PERIOD
        == 0
    ):
        return HashableList.from_iterable((), state.eth1_data_votes.sedes)
    else:
        return state.eth1_data_votes


def _update_effective_balances(
    state: BeaconState, config: Eth2Config
) -> Tuple[Validator, ...]:
    hysteresis_increment = (
        config.EFFECTIVE_BALANCE_INCREMENT // config.HYSTERESIS_QUOTIENT
    )
    downward_threshold = hysteresis_increment * config.HYSTERESIS_DOWNWARD_MULTIPLIER
    upward_threshold = hysteresis_increment * config.HYSTERESIS_UPWARD_MULTIPLIER
    new_validators = state.validators
    for index, validator in enumerate(state.validators):
        balance = state.balances[index]
        if (
            balance + downward_threshold < validator.effective_balance
            or validator.effective_balance + upward_threshold < balance
        ):
            new_effective_balance = min(
                balance - balance % config.EFFECTIVE_BALANCE_INCREMENT,
                config.MAX_EFFECTIVE_BALANCE,
            )
            new_validators = new_validators.transform(
                (index, "effective_balance"), new_effective_balance
            )
    return new_validators


def _compute_next_slashings(state: BeaconState, config: Eth2Config) -> Tuple[Gwei, ...]:
    next_epoch = state.next_epoch(config.SLOTS_PER_EPOCH)
    return state.slashings.set(next_epoch % config.EPOCHS_PER_SLASHINGS_VECTOR, Gwei(0))


def _compute_next_randao_mixes(
    state: BeaconState, config: Eth2Config
) -> Tuple[Hash32, ...]:
    current_epoch = state.current_epoch(config.SLOTS_PER_EPOCH)
    next_epoch = state.next_epoch(config.SLOTS_PER_EPOCH)
    return state.randao_mixes.set(
        next_epoch % config.EPOCHS_PER_HISTORICAL_VECTOR,
        get_randao_mix(state, current_epoch, config.EPOCHS_PER_HISTORICAL_VECTOR),
    )


def _compute_next_historical_roots(
    state: BeaconState, config: Eth2Config
) -> Tuple[Hash32, ...]:
    next_epoch = state.next_epoch(config.SLOTS_PER_EPOCH)
    new_historical_roots = state.historical_roots
    if next_epoch % (config.SLOTS_PER_HISTORICAL_ROOT // config.SLOTS_PER_EPOCH) == 0:
        historical_batch = HistoricalBatch.create(
            block_roots=state.block_roots, state_roots=state.state_roots
        )
        new_historical_roots = new_historical_roots.append(
            historical_batch.hash_tree_root
        )
    return new_historical_roots


def process_final_updates(state: BeaconState, config: Eth2Config) -> BeaconState:
    new_eth1_data_votes = _determine_next_eth1_votes(state, config)
    new_validators = _update_effective_balances(state, config)
    new_slashings = _compute_next_slashings(state, config)
    new_randao_mixes = _compute_next_randao_mixes(state, config)
    new_historical_roots = _compute_next_historical_roots(state, config)
    return state.mset(
        "eth1_data_votes",
        new_eth1_data_votes,
        "validators",
        new_validators,
        "slashings",
        new_slashings,
        "randao_mixes",
        new_randao_mixes,
        "historical_roots",
        new_historical_roots,
        "previous_epoch_attestations",
        state.current_epoch_attestations,
        "current_epoch_attestations",
        HashableList.from_iterable((), sedes=state.current_epoch_attestations.sedes),
    )


def process_epoch(state: BeaconState, config: Eth2Config) -> BeaconState:
    state = process_justification_and_finalization(state, config)
    state = process_rewards_and_penalties(state, config)
    state = process_registry_updates(state, config)
    state = process_slashings(state, config)
    state = process_final_updates(state, config)

    return state
