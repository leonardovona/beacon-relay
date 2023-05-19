"""
This file implements a very basilar light client for the Ethereum Beacon chain.
It uses the executable specifications defined in
    https://github.com/ethereum/consensus-specs/
and is based on the description of the light client behavior explained in 
    https://github.com/ethereum/consensus-specs/blob/dev/specs/altair/light-client/light-client.md 

Some parts of the code are adapted from:
    https://github.com/ChainSafe/lodestar/tree/unstable/packages/light-client
    https://github.com/EchoAlice/python-light-client/tree/light-client

The life cycle of the light client is the following:
    1. Bootstrap
        1a. Get a trusted block root (e.g., last finalized block) from a node of the chain
        1b. Get the light client bootstrap data using the trusted block root
        1c. Initialize the light client store with the light client bootstrap data
    2. Sync
        2a. Get the sync committee updates from the trusted block sync period to the current sync period
        2b. Process and update the light client store
    3. Start the following tasks:
        3a. Poll for optimistic updates
        3b. Poll for finality updates
        3c. Poll for sync committee updates
"""

from py_ecc.bls import G2ProofOfPossession as py_ecc_bls

# specs is the package that contains the executable specifications of the Ethereum Beacon chain
from utils.specs import (
    Root, LightClientBootstrap, initialize_light_client_store, compute_sync_committee_period_at_slot, process_light_client_update, MAX_REQUEST_LIGHT_CLIENT_UPDATES,
    LightClientOptimisticUpdate, process_light_client_optimistic_update, process_light_client_finality_update,
    LightClientFinalityUpdate, EPOCHS_PER_SYNC_COMMITTEE_PERIOD, compute_epoch_at_slot, BLSPubkey, BLSSignature)

# parsing is the package that contains the functions to parse the data returned by the chain node
import utils.parsing as parsing

import requests
import math
import asyncio

# test
from utils.ssz.ssz_impl import hash_tree_root

# clock is the package that contains the functions to manage the chain time
from utils.clock import get_current_slot, time_until_next_epoch

# Takes into account possible clock drifts. The low value provides protection against a server sending updates too far in the future
MAX_CLOCK_DISPARITY_SEC = 10

OPTIMISTIC_UPDATE_POLL_INTERVAL = 12
FINALITY_UPDATE_POLL_INTERVAL = 48  # Da modificare

LOOKAHEAD_EPOCHS_COMMITTEE_SYNC = 8

# Fixed beacon chain node endpoint
ENDPOINT_NODE_URL = "https://lodestar-mainnet.chainsafe.io"


def beacon_api(url):
    """
    Retrieve data by means of the beacon chain node API
    """
    response = requests.get(url)
    assert response.ok
    return response.json()


def get_genesis_validators_root():
    """
    Retrieve the genesis validators root from the beacon chain node
    """
    return Root(beacon_api(f"{ENDPOINT_NODE_URL}/eth/v1/beacon/genesis")['data']['genesis_validators_root'])
    # return Root(parsing.hex_to_bytes(beacon_api(f"{ENDPOINT_NODE_URL}/eth/v1/beacon/genesis")['data']['genesis_validators_root']))


genesis_validators_root = get_genesis_validators_root()


def updates_for_period(sync_period, count):
    """
    Retrieve the sync committee updates for a given sync period
    """
    sync_period = str(sync_period)
    return beacon_api(f"{ENDPOINT_NODE_URL}/eth/v1/beacon/light_client/updates?start_period={sync_period}&count={count}")


def get_trusted_block_root():
    """
    Retrieve the last finalized block root from the beacon chain node
    """
    return Root(beacon_api(f"{ENDPOINT_NODE_URL}/eth/v1/beacon/headers/finalized")['data']['root'])
    # return Root(parsing.hex_to_bytes(beacon_api(f"{ENDPOINT_NODE_URL}/eth/v1/beacon/headers/finalized")['data']['root']))


def get_light_client_bootstrap(trusted_block_root):
    """
    Retrieve and parse the light client bootstrap data from the beacon chain node
    """
    response = beacon_api(
        f"{ENDPOINT_NODE_URL}/eth/v1/beacon/light_client/bootstrap/{trusted_block_root}")['data']

    return LightClientBootstrap(
        header=parsing.parse_header(response['header']),
        current_sync_committee=parsing.parse_sync_committee(
            response['current_sync_committee']),
        current_sync_committee_branch=response['current_sync_committee_branch']
        # current_sync_committee_branch = map(parsing.hex_to_bytes, response['current_sync_committee_branch'])
    )

from utils.ssz.ssz_typing import Vector


def bootstrap():
    """
    Starting point of the synchronization process, it retrieves the light client bootstrap data and initializes the light client store
    """
    trusted_block_root = get_trusted_block_root()
    light_client_bootstrap = get_light_client_bootstrap(trusted_block_root)

    light_client_store = initialize_light_client_store(
        trusted_block_root, light_client_bootstrap)
    
    return light_client_store


def chunkify_range(from_period, to_period, items_per_chunk):
    """
    Split a range of sync committee periods into chunks of a given size.
    Necessary because the beacon chain node API does not allow to retrieve more than a given amount of
    sync committee updates at a time
    """
    if items_per_chunk < 1:
        items_per_chunk = 1

    total_items = to_period - from_period + 1

    chunk_count = max(math.ceil(int(total_items) / items_per_chunk), 1)

    chunks = []
    for i in range(chunk_count):
        _from = from_period + i * items_per_chunk
        _to = min(from_period + (i + 1) * items_per_chunk - 1, to_period)
        chunks.append([_from, _to])
        if _to >= to_period:
            break
    return chunks


def get_optimistic_update():
    """
    Retrieve and parse the latest optimistic update from the beacon chain node
    """
    optimistic_update = beacon_api(
        f"{ENDPOINT_NODE_URL}/eth/v1/beacon/light_client/optimistic_update")['data']

    return LightClientOptimisticUpdate(
        attested_header=parsing.parse_header(
            optimistic_update['attested_header']),
        sync_aggregate=parsing.parse_sync_aggregate(
            optimistic_update['sync_aggregate']),
        signature_slot=int(optimistic_update['signature_slot'])
    )


# !!! eth1/v1/events allows to subscribe to events
async def handle_optimistic_updates(light_client_store):
    """
    Tasks which periodically retrieves the latest optimistic update from the beacon chain node and processes it
    """
    last_optimistic_update = None
    while True:
        try:
            optimistic_update = get_optimistic_update()

            if last_optimistic_update is None or last_optimistic_update.attested_header.beacon.slot != optimistic_update.attested_header.beacon.slot:
                last_optimistic_update = optimistic_update
                print("Processing optimistic update: slot",
                      optimistic_update.attested_header.beacon.slot)
                process_light_client_optimistic_update(light_client_store,
                                                       optimistic_update,
                                                       get_current_slot(
                                                           tolerance=MAX_CLOCK_DISPARITY_SEC),
                                                       genesis_validators_root)
        # In case of sync_committee_bits length is less than 512, remerkleable throws an Exception
        # In case of failure during API call, beacon_api throws an AssertionError
        except (AssertionError, Exception):
            print("Unable to retrieve optimistic update")

        await asyncio.sleep(OPTIMISTIC_UPDATE_POLL_INTERVAL)


def get_finality_update():
    """
    Retrieve and parse the latest finality update from the beacon chain node
    """
    finality_update = beacon_api(
        f"{ENDPOINT_NODE_URL}/eth/v1/beacon/light_client/finality_update")['data']

    return LightClientFinalityUpdate(
        attested_header=parsing.parse_header(
            finality_update['attested_header']),
        finalized_header=parsing.parse_header(
            finality_update['finalized_header']),
        finality_branch=finality_update['finality_branch'],
        # finality_branch=parsing.hex_to_bytes(finality_update['finality_branch']),
        sync_aggregate=parsing.parse_sync_aggregate(
            finality_update['sync_aggregate']),
        signature_slot=int(finality_update['signature_slot'])
    )


async def handle_finality_updates(light_client_store):
    """
    Tasks which periodically retrieves the latest finality update from the beacon chain node and processes it
    """
    last_finality_update = None
    while True:
        try:
            finality_update = get_finality_update()
            if last_finality_update is None or last_finality_update.finalized_header.beacon.slot != finality_update.finalized_header.beacon.slot:
                last_finality_update = finality_update
                print("Processing finality update: slot",
                      last_finality_update.finalized_header.beacon.slot)
                process_light_client_finality_update(light_client_store,
                                                     finality_update,
                                                     get_current_slot(
                                                         tolerance=MAX_CLOCK_DISPARITY_SEC),
                                                     genesis_validators_root)
        # In case of sync_committee_bits length is less than 512, remerkleable throws an Exception
        # In case of failure during API call, beacon_api throws an AssertionError
        except (AssertionError, Exception):
            print("Unable to retrieve finality update")

        await asyncio.sleep(FINALITY_UPDATE_POLL_INTERVAL)


def sync(light_client_store, last_period, current_period):
    """
    Sync the light client store with the beacon chain for a given sync committee period range
    """
    # split the period range into chunks of MAX_REQUEST_LIGHT_CLIENT_UPDATES
    period_ranges = chunkify_range(
        last_period, current_period, MAX_REQUEST_LIGHT_CLIENT_UPDATES)

    for (from_period, to_period) in period_ranges:
        count = to_period + 1 - from_period
        updates = updates_for_period(from_period, count)
        updates = parsing.parse_light_client_updates(updates)
        for update in updates:
            print("Processing update")
            process_light_client_update(light_client_store, update, get_current_slot(
                tolerance=MAX_CLOCK_DISPARITY_SEC), genesis_validators_root)


async def main():
    """
    Main function of the light client
    """
    print("Processing bootstrap")
    light_client_store = bootstrap()
    print("Processing bootstrap done")

    print("Start syncing")
    # Compute the current sync period
    current_period = compute_sync_committee_period_at_slot(
        get_current_slot())  # ! cambia con funzioni di mainnet

    # Compute the sync period associated with the optimistic header
    last_period = compute_sync_committee_period_at_slot(
        light_client_store.optimistic_header.beacon.slot)

    # SYNC
    sync(light_client_store, last_period, current_period)
    print("Sync done")

    # subscribe
    print("Start optimistic update handler")
    asyncio.create_task(handle_optimistic_updates(light_client_store))
    print("Start finality update handler")
    asyncio.create_task(handle_finality_updates(light_client_store))

    while True:
        # ! evaluate to insert an optimistic update

        # when close to the end of a sync period poll for sync committee updates
        current_slot = get_current_slot()
        epoch_in_sync_period = compute_epoch_at_slot(
            current_slot) % EPOCHS_PER_SYNC_COMMITTEE_PERIOD

        if (EPOCHS_PER_SYNC_COMMITTEE_PERIOD - epoch_in_sync_period <= LOOKAHEAD_EPOCHS_COMMITTEE_SYNC):
            period = compute_sync_committee_period_at_slot(current_slot)
            sync(period, period)

        print("Polling next sync committee update in",
              time_until_next_epoch(), "secs")
        await asyncio.sleep(time_until_next_epoch())

from utils.specs import BLSPubkey

if __name__ == "__main__":
    # asyncio.run(main())

    x = Vector[BLSPubkey, 512]("0xb829e2a55b46c3cfae524d4b3bbe6f54610fa598581d0cbf026ca8e8ab1967b17d6fbdf154dc32ffe7ca18ec6094d4bc", "0x8200ca0f25a216263a6fab66bc19cfb9e46682ae474472565f5322b4edf6f22d71a5c2933622c0ed5e6f3c2dfa0c9d0d", "0x83a5a0529f28496a5d3963960f43135d9ddc6c1202c7073cd4d028853b9c054cf3abe26a96f99d56faf22aba8a729af1", "0xb5311edf5e7a92b833ff8d9bcd4cd94823b01b4a91b61ce1a3008e7e6e397992ecfe99f412e00443aba81e988d3b8848", "0x91a36cf604e62e30fb894e9af717cf75bddcf26a993ecb13fe50124ba2d0e6294ca034e6d1b233cfeaab8e084116c59e", "0xac325749c279d06131a5fdbce1ab645ddd08b7449361a18ce6be4443e4de079f69c092f9179f53e68f4fd1ab14e64be3", "0xa55caf8cde2072e01f02129f1d6d0847cf23f0008e0e63601a05434b26a6187c60cb3f17b696ba2a774d96ff25a20ba5", "0x8dc1a518e4a60fc934cd2dfb8ab2b336a8d2ef4300262f14fec18445ed0065429d863588d356202b61880816aa144ebf", "0x857ee7d3b61497e1b0f1c96b8b37d8883fc4f79cf48b655e052edeb7a2319e71465b7737390369b426d01d8480a2560d", "0x8d98edb1980d7689fec39a0f9bfccc4147d5b8418483c44af61614830673b48dd0bd052b64c4fa78833b853c30246f44", "0x832e0e9c21456dad039b9e5e7256d06bba68b4ae45b24453b8a974b705979a57d36d9ae70b0bc0d725cbed5a899c5099", "0xb8365ef849bb99c639ffe3c0d93b9f26c041742a28bb527f501c279642a59c10c85bd09cf7179342bea2490e8e8f81dd", "0xae1a6f138802eb0cf60dc4e6d97505b14567317ed8a004c68dcf4b29b6eb392b48759b1812ba0173aaef0162db4e49eb", "0xb3fe71fd53064643639712b0bb8ef47ea2e42fa8893885013f373ba00b9fc96327306d347543fa4e9d1f44547ac3d15a", "0x97047709247891920be5a88c4277940260f88618a714ad78072cff6d2f1f4704174bc35c7a103fc7db9ce6eff1e246a5", "0x93b65786f76b08414d0cd493cce925aaca6fa4d1e9314c99747b6b07798a9da4263c1b0553e617a78bcae364f6e5eb49", "0x862fb548a044c297483df431427438064c1d7f274abbeea629ebb040870c158484ff4d65a53690240bc300d232d5b3b5", "0x969724e16c5ad9544ab584e17f58da9a3b371e1e47e245ac1cee9716ee007f44262bab9790a7a9ea8e1b6417907a1c5c", "0xa1c108250f26d21e075d445dd66de3972da134c2b3557c841126b25c09de4b377df6e7525bed0e274c3e277c3232e0b2", "0xb77adc706f84fd5040c4ee5d64d7b1cd40618ea8fedf64d7db537fd50efd83d5bc546c544a8aa08dcfccae2e4149679e", "0x8615ea3d2d5a26ad5842ab4723723ad96c72a43f5ccc93251684253dcf318c46e02af7bbda32224a858dce66aa950fa7", "0xb01ae8ed2021468a91a149becdd5c4f4460f9cd115e6bffd4520cbc70e5cd5564c550ac7f45966f18000b1db709a66f0", "0xb7620b2f8f68d8a84fae4e2cbca4dcc36960507c6648ec59e5e5d7b31ad3b1f21d278c973f4dde86af59d56ed775ddbc", "0x843067da9942eb347650bb801ec97d44177b39cc97d90933559ff6b28b638f228a1c5a5da5c51cf4f87dbc5f7fa62978", "0x93cea2757d02283295df88d8fc66c919e924048c84b5c2915faba3fe0efd33984d2f4da8f1e03bfee3d27aefc7ce7a6e", "0xb0bc6e25da180e0516f9ad274ba2998893a1ef7e0405a0f5f27bad34fd3b593873f5048fc1968ae7ec30d9b4c5a80b06", "0xa01a100da93a995c68b79e932eefcd68dc68fd080cabcc89d63d5634262522eceed7f237903a328e1dcc8ea72ee65d3b", "0x80dd6c85ee9aa5302399b5bb675a9cbb52830d870cd3be3572bbce319e208a77fe4322455ef6a2f9ebd45c55e52be599", "0xada7cd861335ff5b5206d63cd0a7cd78ba2a55d41ba6a4c54335e13824a23270b5eeab69fe48f0ce3d63cd3b88122082", "0xa570717dcd5e2d54c708c80a7c8b4e7293d8d9e42cd222dc144573c25fc5e1804e1e62e8f8073826675b3c235ad35ca9", "0x957d53db832a6976e9100c7d31f275d502cb9da0be0781ef37a7edd344a141266bb71c37f531a7d3d448e5d9392bec8f", "0x8175ea90c8b2c3a4472acc97dfa6857f64f376dddaee2da4deb601c5dd1c9b41db6c41dec5640504842285ef768645d0", "0xae3c475dea761969c7a47188c716e7aeacca4dd342959c19d8de1a509b2250aee38df04726574cc81b30989c5b054eb5", "0x89f7fe95bf1ff60ddda018553d72aa839ad820f107b00096f296b55c6176436a58adc0be6f5830a8f0c74b5c3690f9f8", "0xb43163366bfb5aff8ee22bcf50862a7dd428989d3a5f43816880606fd94f2c31ad97dd27de55a63be60cad22fd65d934", "0xa22274e3d73604e773c04ecbd89d15e061810d9f3691f28b05751ece7dcecbfa523755028995186297e70800937266df", "0x8b2544880e8ac33ae9ae377523d415aded101bf63b2652c0b1265bce6d0ed4c6cf044e38eda0f993b9d8f3ca741ae8c0", "0x8c32a7cd0b77dda73c2aff180391e944df185354b130b99dbd2070dbf3a33baa56a97700d4cfd3a41af279b3c17bdc3f", "0x924042a76249a7c9c84471a34895e2e8ec4e11c45ce346d1d234f1c259bb9f23184c1551d42066fb000ca70fabc1927d", "0x84a056203216451a35077bdb73fb1fab69e75065a30089f9a9454844f1f7c7d8ab1451aa723079c8a0b77774244edeb1", "0xaa33a2003e78c7c9b9f03951b2bce71476cbabb43c908b84f17fd553b088aff0d8f3d434a02c2c8f19e7424622335f25", "0xb4f077dda621c62b59c8c925b4e60dab740aa611354bc2cd2889d6ef90b1a31d9243a4420ae41c7ebdebc09e02f7fcc9", "0x911a20aaab26b4f868b6167c161cf2c2797a2c4d981564f7e07f73a7be365fd7533ccd871560b6a34f49807aedcfcc30", "0x879c0df426988180d9a2e7fc5ee8e97b0e4567b55aee9ac54082f16e7b26ff8cb1b98ea31c218b95a6985545d89cfdd6", "0xadb4b8ecc5bd4a6893d37dbc7bf6fddf5cd8aed7242a188ae1f12ca2b25068e6ad1b7ea8f2301860dd2a9b9deb441e14", "0xad0fa1ad4f1163fecfcc352ebe2f84bd57102e7f705c70d97b721677b1eefd0e0034524494baca9923f91ad01ba9c73a", "0xa4035cb07abd659b6210efc57eef38ae5aea78dd35215bcd3b4edc3ae0efd9b7391f1f5a7c8c24abe0f0d8055794e88e", "0xae278d230823ccaf16f9eacb6eac52abfa8358faaaf72c5b8d5dc319e841b54edcdf7e7a647588ebb66522d06530d6a6", "0x9303f1168baab6f7ff9880ee9d516dab3efd79768b0a4f329e2d309cdb1d6a058190d08432fdf5d6ab0366ce2f2b41b9", "0xa82618c399215370a7139a0fe70c39fe7a2f6aaa7df240390e2dcd3deef8dc7625c3c1ee914fba506254ecf146f84399", "0x84af0546f2d7d661cbda0fe5e27a8070c23bb6a8d518f7bce0c7e37ec90797e0af737134eb4ca83d4112ffef2d8467c9", "0xb8de836b9df1ec96447cbea028be84b32cbc0987ab0663e9f82c23a5fba36ec44d111395992a9cfb043ea973d71e505a", "0x8879d84e598a2ff32c281722c10d41d6f8bdaea803a5edd32db3e69e7014f15ab1f1a0d650d94cded43bdf66d32feba1", "0x8e03c291656d52eb2b1bcd2119e90b46da662c34e69a4bd26673fc095539f2e75415b622e276525a738ccdabe4981808", "0xb10e6b255e407d737cfd9719fed0c46c8d1bfab10a6cf33ee1afa62af6c39dce54d056c74792b1fbdddcbf4cc68019bc", "0xa71cced86482b2898084ebc3e1b0b0de26dea7d9afee7a7628a6a69784fecd22fca0b1b163ac4830f99c6b32fcba5460", "0x81530ff0faaf18a948cf320a501a9b907a791b01738704e2ca4f2f6db081005f1f8acacdcc94fc117699185daaf53251", "0xa4874581b101fd8c1624c5ed66f8e8b1d1078cd870b91232d43281f36dd5e063d272504f6099973b727f8c98b6035bc0", "0xb3ce22bcea6ad0075a11b050801311ed1128e7a155ae320f0e8b3e351db844bdc1ef69cdd83c69706fcf2f7046ecac6d", "0xa2f6c2d3847d5993c8a426dc1b5fa2007584b386bd5bb07cec1aaf7fd79683fb578b64fdede44af0bd0fd98cc78b30d5", "0x82b6be13a155df93e686babc44baa0f957f08f79c9e3606b8053d7518dd418c6cfe27bf88d88dda178b8813e50c3b7d7", "0xa67809bcb7f6247c3b78111e551e5ca2d91b0a22a28dfec97dc3bd87de772ed66754684a4f674d6124a91df53ff9785b", "0x8c1db07d004357c68c5468e6cf9be92c2897f4bb3d515ce4e0076b209bda500f8f68c7d75e85764055a55984b9c6e97e", "0xafbb62fb4e03b5e7dabd2d397922dc214bbbbee9475d5010e077b8caab66194c6f7e50b4305eac4ad21e811969a0326c", "0xb4b94b8efc62250f01230d8f26aec31fbe69dd438daedfc809cf927857a266da8e403e7856c0b36b706e3807314d6154", "0xb7e924f7273001a39e762029f3a5d761c4dfc8771bc8436a8ccc580f5a5f495ffd46363105220e91ca0740565a52a07c", "0x93e6cd7345bc1db4af6ee2f17d5218b2c4ec9e5401f03d9032edb5d0d81aeb11dcaa1733a2541f8d9356cf305a50f804", "0xb78ed995565148211b840ce095eb831e3db592a5262801754d26232d5ad33b04fba9a0fa5efba302f539245d35934a87", "0x8f1e9fcd9d13b531d2a6b53ba9000ef076efc699bd7c2f12949535e30fde3116ee7d092015aa9303bc4b53f7f72e5312", "0xb187075262db68eebe336fe8943e14647be06651c0e44497a6197e6fddcdce59a1fd2f8f69a441ae2defa38e18984e73", "0x81c22fa6b2d4e34c8fb66d79cafb516089c00e793ce4c01730b305f33b98cd08bf7b86e2bef4a3b652710c988ac0bac4", "0xb12b6c5ea8f51e91e7268644f8594b37bf6c8765eb24ff39fe0669110dce0c72bad427c1b077b5bf2ea9d3bc86d783c5", "0x99ce084c48608ce246948349ac444ed7bb524e998ea76b27d54f541b02db1e3bf25ee0cbcea042f1b900e2f59783e63c", "0xb6dc767a5d4460c803edbe5d2c886930e0e13dd9c1ed71fb31c4f5bad9d527ed2d944cca87d3ccf20a9c59a84ad55215", "0x867c7117ff696c066b0b1096fc4652a5b8be8e1826cf46b382767b9ef5cddefcc7368a67831feb41da7ed7592a6f2afa", "0x857f2be30f3341509863a90e4343a8daf89382ffd0b9e974ab41573665cddf95f6992ab454c42e7f4f8ab2cbe918306f", "0x8a4c9790a6586819f98a1eef118216510af15852bf81d785c2144829bc752836ad3a46e2bb06a9c6f36ce7bb6208245a", "0x89046dcca395855b567ed9278a27f4995586128c808aa07b2dc6755e13496ed3b2c32e271f2e63a1b34be6999d3fd34e", "0x8a49e7565ada456d408a023de8ca93a6160fbc0ac2f8302bbcc2c8b999f1318704029362ff95aa86acf64ffbaec315aa", "0xa8969ea8ff3d58f1367d73a2de026eaeaeff75544900fa1b6f621efdd80305b363969c6dd9ee3dec14481d9dcbeb02be", "0x8bf9d1c505902e567d2220f69951e1d56b5449dfae38bb22575062d47a0522234954efc867180f7d9b61ad19e6ff8ee7", "0x88253c9b55ef569343cc7b07e5b1dbf70fd936df25cf10fcff73c5e8b11c9c30445ccf42ac772d0236c96623ba7c8533", "0xb2c5dfa2581089184cfb5825308155661689ccb4df39f4513493c6d501b84f7d83cb984e5e56fd3901df95fae202aca2", "0x9657138af39dc61ba7d1285ab225de3898fdb9954f056ab7c37cf4147508e0ea8141c81acca7ead5a3911eb1970db279", "0xb96630caa40c36b24452b25aadba608871f386d6c9be9f6deeacb8163622f12da2e08188242a465cc6f64f5f24aacb1d", "0xb0f5c334581b2d2d80f63190e74065ef80dec3b154de3e2fd82c78296b23e3348e5b4c13ce1d1c7e3d4c5fe8a8d6d72e", "0x90937fd4da0066470445f004cf30e836f55fd62467e762584bc894e141fdc8e88e13bea3aac41818706ace6fa7ce872c", "0xb98c9631a64aa5029ec123173d0bdd0cfef6ba8227599f442d3cf84876de94777c9b8de670289715368b41a9c2f9c587", "0xa108e117c75d72ca03cdde6554ec1ff4d1a06786ef27bb684e43b926fb9ad3c8e44876d55f9f2730077d5420f620cac0", "0xa289e011ea42d3fb859f3a348b8a1238e1fc45ca38099aa812ea1b665228339753d6f6eb8cc69995d441559e201f8eaa", "0x98320c8a09e7d0f23c3acc1ad6fa8cc3b1472f9fdec6f49be9688e8289421ebc21c24ad137963414aaddfedd6a4bcb65", "0xaa8fa0b2375e25888ed1723afa983761c4fffe1b7f1702b7e8f8e51dcd2eb8ab9317c9ece256fec8486a4f299f2b07da", "0xb54dc951dfc7ea974b1c497156d8d36f02caa371e837c8363e22dbaef31ec0f9a28fa3db17bde133a90209a9c9d31509", "0xb8c577bc09899977d431de572e00b34842893665288b10fdaed9a723fd3de606762e477898da19360bde1cbe7915c687", "0x81f4eca4f8e9e19ed1d5c500c50678bbfab0e4088d0bc006fb71aefb0065cd549dde688d772bb1ab9f64184b450090e9", "0xa3e397dc5986a620ac1521aaf3eeba7a2375d991d64078520c5a06b256923184e18ece5d12fb0422cc42767da8983d92", "0xb6c259ce940cb5087a326e396e1167b943640b0134c3041a12de952fc89c4a873b6d11a0b206a252d4b2052c115c6c8e", "0xa6acf905d8e691ed31be12c357a87a2eee50f1e6620eb1452951325a56a8edf161ae592d126fa1791a83bdc329ba6a6f", "0x92a270d32329d7ffa1abc5ff84e9456215fffea12d8cd7a5c68e7d183652b57ac2567b8fa1cea9fab84d843afaf0a7e5", "0xabf3d35bbd98f8f0e69999af84efea3add6fb7fac589755e46193c1008270f9d1579c9ee9dfe7c1a49401a1748518b4e", "0xb9416aaa5ab52925873b8e1a787fb9c2171cdd2b2fac39044acd845f71a696f3ea645282155644a76ebc7d2e8856ffe4", "0x859bb6984beedd45f7d69e50ea43e05cf5c5814ecb431bb037c981909c437f980877edc44b7f8a987b3f3397085ae650", "0xa2046b042d2fb69455ab1f20de2ffe2b003da988307fcd2e1b7b1ea499bd4dcc6521d06ffe77346f2745b7e96ad1e35f", "0xabd08a7dc4fd9c90e6601d15794f5e7e62cc0ede15dbcbb96f4b644f13325bc3fdcf4eac858a4d52817672bbb5fa2afc", "0x8e5642b5fc61e21a6db0a375846e20cebf9efe58ba131a675e96b106e873ee65eb8bb27147e2262f74d1881f5d2f6369", "0x9277367d567f3fd25bcee07592fe832990b426ace64e9e488a99677813c6aba184e9c3e74706e797d45016d7786e7cd2", "0x947a06f00aa9071b9b95e7eb5346a8766c0e8c41a904a1cae492f2479459b6b0c30fb1e21208d4a7733e7a6a879987ea", "0xa78478d920620c61dddf28c93967d8d4dda6a373ce8d7b57e4d319b6bb6432f48cf2bc5938f9df46b606bedb68faa7f1", "0xa1319ac0d2bed9e38d29e6965c59917235e9dca415e98fc619000188fba9e77528a7634e557d428e531408f03d932eb7", "0x816d7278ca97ae4a31c3114a73bd83d24b925fe325ad5a1c088fe1c1fcaaf8ea609a50ccfa14cc52765e899826337a50", "0x8e9ba67eb4854b54a45ec366e1968e3ee7d066568ea79ac6306ed142b6e7e445bfdaca8ed3fc8dc9c2613c26769a3d96", "0x94a2f3f9f6ff4a9d0b059e771d24a0ef28bc4afe9fbbaeb1fa72a87620de23b951a2539e0b298ce6f27b35d9d1792d12", "0x908010938fa01e6b53f2e5d5b8d3e7639af0b9c2e1c7b682c1d7287f5e0cec1fb7536908ae7023c5f97b3943e54462e1", "0xae4bcc49ff2cfc779c639b3943afa51cc1daf85d45f2ce46d55520b359482ac2f8c92306fee7f3637c3a9809b528bb95", "0xb414a1e8a8025e1985697d1b3f43170ce67e43d8b52aed66d9f32eac24c22d173b9640253891088f1dd5bfec81536026", "0x9392abc26cdb0befdb669531a97a8a1d33bf94b6961ccd8da641fd757ffbc02daf46574648313b8e2f6d23c114cff684", "0x81530d79aea9c678102754fdb4692683a914174ceaaa596dc2c9a2036d037bfd13758349929d98637e9a560f08416420", "0xadc6d3a1bca3a5d5bf0bee59fc8433b3022ab0f103d875c1eb0815a91c980001f75daae7599deba734bf81e1a5eb7283", "0x92500bb4a3e0d0973313e1aef587c5e3a59cd86def6e73731598a00adeb34a82afaddb9f87dbfc72427c86b84ddab506", "0x89e025afe9684e8cc883314ef83ea9461bd2375485b009c1bfc73e1bd8690182b4914106ee0c890f89c3a372946ddebb", "0x8e8b4351750606fb648e32033b3a4e415476fe1c2f3a6050d2c8d0a1aeba4c5d3a2f2fb35b27a9851884e8dbffae3fa9", "0x8042391fad9eb19f1aad9ce7b470fff1994b6aa46b32b7465b69f0659dbef3e9e13552bc2abd8e7c4080b38ab2014f60", "0xb820c8ba383bc9f24373fb6c160bb8204f051c6ea56082346925f9ae161bcd10ab96e1cc8b63a53a3c6e1db4c529428d", "0xaee13dcef7969788dab9cd9280b3f006759e97eab98afed54c678f58fad25f4fd482a52e62f8743fad89419944047007", "0xb6f2695f15bd2a945d726633469be3232e2f8aa037830811ceeb77cb7202db0959b7d2402cf35326a7f76aea1fe06fb1", "0xabcba10ce38178c3136ef120762ec7b0f8891d756b33c5caa9e5d74d3b088a6643e22100c5d7d21ad7a5a8e839e1073c", "0x8c1398b3b0aa6e5711f99f4f0055acb7da2419f2a806212f22159fcb7edfe7614e3002aa612fb947f84760298c25aa3c", "0xa9a1ccf219cb5f9be11eae2cc51948e1f3e000d90204ec6b2265a993861143b0463965c52e1d1f4822ba10b967fe81c7", "0x99d90c2fcba006c27dcb5bd5a10cb47b981416694593b81539d4ca55acfefffeea592a5f53493b9126a6d621f03fac31", "0xb8268aaaa2d61679e498f15b3091964182199cbe6d99091a7f7c1ac8d98b56c3b96da14dc0f917756947ad32cb34f500", "0x822ac79457139f381dc9762366137200ddac825b5f0744981bfa2e5f7b8ef31a3a558b5e37c32831ada7e4047458e09e", "0xab1cc28ac84ad84b13c40841792698c2cf41d76265d5f7901f8ac6926bf048272b89129c1d1dea57aeeace28dbb8f785", "0xb18f9e5eee90369c9532097d08d1cda3dfa8ebdf558e93fdf4bbc7f44c12d4ec2a8561194be63f1e39555ae3b88e7b84", "0x9605e309897c5586e87ae31a0af57f9d7e66e35d008155e5d76258ab80167df3e928318f6f503d9b9a9cbae17fa3a745", "0x838c38e8ee751a1f0f1c34beac9e2bb9d58f836a86cde06ce6b3cb3fff0e9999f78d517af55887b15846f464fe1851f5", "0xa517db2aed8d989026a5bb6e746dd1ed91d6724ff4132fe1285b5278c6df9f5676dc6bffd3d2b54cb121dfcbfb3afa46", "0x80b209deb6e22950b038eab15e03a3036bc45185675f4018d5a5719c545ea2bd30c5a6e2e7e4fa7b59678ec89c78d991", "0x847fc2efe6a6682756904c0047b9de14f3e61bb85a861698498df24d028a3d4a6a8b1329052ac987385cde5b8ea84fda", "0xa6d3c1693303619aa5154dbfe5c93c5b76dd068bf62af38f0170303f56e51c9fccf51c495a6ac6240922cd7d872d95f5", "0x823156a1fd511c88a32698cc11254d4ad80e788ba9500b6f1d02be95c864e10af11da51d895bc1679b11d281f5f9a1cd", "0xaf4b7c671f199bf7d081e809abdcdb969a0ba5bdd9fb8e1662899ed341f970c0bf143ab99e11ff23d91e28e993385d01", "0xb47e55cd7d431689bf81bbde021e95d838ab5b2ecf61ea47e386802524a0fbdddea832ed16fb0587670a8604cde0b67b", "0x93c717321e08c4affcb7b8c32f4d3706068990b50b8037586825a1a6b2f5fa001540887d0869918e0984d2d6471566ab", "0x8b82a556884414c6dd31019ceff2ec43c6d56cc2ef6f4e0d2d31788d16fe14f149f2d39810388b245b9f6d2228c5dd22", "0x8a03e71797eee3caed20b765fe5408e2fe471fc0c4fce4da9ccafdb840848134440bd91cb0b8b891da3d7562ccfda3dc", "0x8ce1e132f3d21899fcfb4b876c47880623de4ecc525c149c05c82c2eca6b706133d7480926f600401d5814c78c9b23ce", "0x89fdf77da2347c348d6ec91d890cffe72eda8cc07672a10eb8649c2d2ea2d39f0d3c5d106e17f4ad67ffad49b000e094", "0xb0dbc4475f9df8189cf67772a8e560d2fc7339d9a27467d679a33fedf1b4b376b74a4eec0d54354ab2920618f39a9485", "0xa02c836bf571d4dcde72cb72e28db75438a49b30faacb25a32b858191c07b63ab9d25a178d956c84ba4d340b3c2c6ce7", "0x8ca2a25a22aca0329319cc291ed9e786a024d08f816d6ae20af4ce86aea4ab3ed951988d55daebf06903ab1bb2739240", "0x9780c012c624ffcc75a520a3a69c0266fb133d02dd8f8b62efacd68d7480af6968d88e5d79d717088599d50f0702100b", "0x8020bbd61fca15ff6d563f73e11d6412040193ba7d0e6fa5994799edb772fc8a0dd4230ca4466a444a127eb091d48c80", "0xa92bf0a602448857cc46cb805c6c69a7438ab0e989f57d7ebe2f45031b00b621db421a20d9f1600fe595d1ad8752799d", "0xa274a1c114f6676981ebcf96100d28facf7ec9937deb388953fcc6d30c4c27c761c57e2ad724bbac239b6216b38e9e5c", "0xb4e341d08bc296c0a6638b25bcc8d697be6a2d52223961cd9bea6aa9e5d4e10ac9f1a7340a10eda2f69c2003e5a58295", "0x95fd61e73e13009c0a45a168b1de10f15412706fc58d267b55739463b48f8ab907e3a6c09fedb334fd98a53aa4b4b643", "0x8e7a1237d676d9cebfb70543ec19b3298b4451de47863c1f2f28896fcfb7607a8d4259bf1a5060ca4cfcecafedfcc911", "0xae82f2fb1a0d75fd928612079f8d1b027e4903816b56af36b382d0e728ff2bfe182b4ba6ea916bf7ba3c37ec55032d5b", "0xacc4199dddb4938e69cb0ab4a6eb7a55db60ab970d86de4e4837e4f5c232106486fe0138a95d66c182080af43e515e73", "0xaf9e5dc524a7a8d2dbde5511f3becbbf570045f30a604dc49e6ad9fecc3e48ec97b8e387598b141fc3c4da3e8616b980", "0x88544bf1099397a12d7f5f67aba1d11399ff3c6968987572c78bf435f5a1efcb6ebe76d2d429bdff420325312027a6e7", "0xa59f39302c93c103d1113d1b381464e6fd968bd7436f647cf0f21c4fa155567e580c820f3d3178d275695920f8c0ccd8", "0xaa0e9774dca2d9b77426dd5b2f60427a5a9bb7f909035f0f2d3049083f224594c5ad89fdbd37993e18c470b416dd4fd8", "0xaf421a7aa773abf067afd3a865070a020d7c5f79d541d2c0fd5625c28886854017bfbe807595a9b06a3569c37efed22f", "0x947baf3c2f6cc1f02a4d141259a9dc09579fdcf616477aa9441674aea159dc1745927f9a98b02d577d0df2310e23b211", "0x938445d2975a660b53ad6258cf8f06de74d8b2b3a81db52e02c22c44ba7e0018b4334919b60f9d35ac8e89046333a0aa", "0x9569f6305a375773845f7d4c82141a7718fc6a5ea85c02925d0b39244ea5ee63f293144b3861a4b206c8bf400bd8d7c5", "0x897c7ebfa73f4f965f7b8f1c452ff528dbb37f91cdd1601b3a03fdc4886f9d4fe59007ca40201eaec589926ed28cba4e", "0x83a57471b0e197b73863e604f73047211ce05b50128dd599b59620d68c1227467ca1b1c29b391ac655e9a4354e51c757", "0x82c3f2af308e698020850ea4e31618d720f0cab55af55e80e4e09b3765e1242da13217f0e0a839312f60a15c797b06b9", "0xb76c571f46497f30b27f35019350225d22b3ccef85c30cafa0765d8291e34823c999b1b72ddea738cfe1ea02709e2328", "0xa0a1658bf542f38b1e9c41e7f1f4cc7ac453a68c8e9c14eefd3e8194d29bac5d7d93a68d3ed8f526e3609ebdc2e07b76", "0x91cd4e4daa3ac34e11914f39ae2f55190f2c2d58c64aa5a4c4d76cc49009786d76ef141785cb42c0765a4723c649cbfb", "0x8772093104642ecbfb6efb9673d2f13f6e6618c6e7bbf35069463e5ca5d1bbf17048f1372c175557c5b9370af0fd16c9", "0xa53414e2521be793dddecf04540f62678bfba187c017332ffb2b451ef86e4b4db4d252116418823f0f2a50e9ad849ded", "0xacf06580431e45237b6309f6c4d8b595dfacce7ca3cff81857b055ea74d385dbd316290753ee79181813ba2e52831ccd", "0xb6c66e9bc4225779cbe7027140f4996797da20fbab690faa0826b384d8b6ad49f3daaa61d18d070c6638a16a45272a12", "0x8c140d0a747fba9efb80add9b845538a614e45720e517289180809afd8c57e65b16099b29fbf124ca5877464e4bd1d59", "0xafd7e5f91d3b6a80a4615d26e40e851bce2e22be72621193047df70a4563f7ce53323378ce7b781059b0d6538f9834a2", "0xb362df56571a335df4934deca8c50e1bf3a995a6eac98972fe1f21ba5623a9b1d50600d5d4e6aa8206199a799aba9c0e", "0x8924ff167a2421ee0ea9a8edebcdf36e2d621c8b40e50f86677c82ac38e87a44464a7e0c5f074d1411fbfe815406b58e", "0x9909f3d827ee611cd4c7457533ed0324b9c42e66dea58b8511bd4f63ffe302f70abb82cae5c9f8a80bb7c9fc801b262d", "0xada06f58af93e6544c35760137ba1a1fe236224124eab7ab9e7a47899cefacffc22ca2b6b86f33e36c0ce34cb05d37ff", "0x8f8a054a315999b482150a5cd30396b720ce2fd5fc911f1a62f1d3adb230db715fdfb5d8ddc115b2381ec884f23629e5", "0xaa8748222baeb7323dd41b2133b6c449b45c536b64edfd3191b5c3836dc520289edd9181a9c71b320dcf4c12e94b426c", "0x917eadcfe8c65b27f929d64bd5e82095ea0b25abb53f7dbc90b098d680f631a5a6af6267fb51c2083082ab181bd73bb5", "0x9017b767a0fe4216c811a5d1d67716d1659124d482e6040e7a3d3e57a9d5d6dbf1856f4e3d5c8539d2061d2e9eaa1c4d", "0x809f7baf4839ca0dd94b319c9dca10586a1e4f0601797edfb4bedbc458eb83f2b3ab3ad53ca875863eeaddccba54725f", "0x97c757afc5e261fd5fff72bf5f85db0ed7acfa74072d5bf4a10899518e00e7cf923dc1171c825bbbd8adb49d20e8570e", "0x92d7deb3501aadaa2dccaffb4d20c041daf9efbfdf90d5fa0df49b3adeeffd83631fdb20c1e58089f5c0c205d97c3fc4", "0xb8a53750855585a6b5aaf957da03991ccaa837c6f6900a832ecf31a1824799898bd89dace661bc78af245af09c501595", "0x8f0f5de508a277109fefee8420169026f0e98744b01547423bb6c37519942c7d72101135996935ca4dabd60fa53c5ac7", "0xa3e6c40e38ab2a67448a753eaf2bbd59144aca22dbc849e2634aef773a62e0706dfde2e60f7edb23e23438a81e932f82", "0xb38d66d6a96c5ff1ee9aabf199cdfe56fe07ea2d68246323094cc75d7a51b6415f6e8159367ba911465a706e540ac537", "0x8486a6addbd5b723d535f44d50d61c6615b4c376cf93db41f489c54611197b8bfa51f564db6ccb8c8fb02a80cb792cb7", "0x8a10286da41711896376528648980e276454bafe038437f912511a9193609f217f829b95dd93e32f621239b52fb91468", "0x9601a553f9db1d82dcd6b7e7fecc01b92219f2a2aa6c9e816c3e98bc124991b791ccc83707c93fdf6c832c69d9270d06", "0xa7c0be7f671ed5e81dccf9f947cabc8a13f4fd22719ecb0c2585d206679c426480ea8e3d49d9b1e8e79d0038dad8e392", "0xa3fb50800c0f457864b96624171fa85830f30f4d7a7de0eeeee163d618b7d488b3cfd1fe5d5c7201a256b43e3e1e1b73", "0xa138362ef833054858844d7f97f83e3a75627b9ec35872c4a40ca914384f2da9a6ca789b2e5adfd7c7bfcd44e6fa8989", "0x975903ac958ad6b9fe099e7742008bdd4f898ba1d6f97fd8694308375069eb8fb0fe3f0cb03419c2f1cd23164a1d130b", "0x93098e609e1955e7edd4c8500d1daf48bc1c441d2e0ff21384e268d6920a43a3e70e03c311458a23fc9405a58f3f9cb1", "0xacf58ba88762b7dbba7733ac0f6f0861c3e792636153079ed5a78d45006b7c8223a78153cd40628096db5ea5e0e0d7e5", "0x8a6466679be692649c9e428cc67bd79dcefb83ac575e0c9ff8e7857addbdc902512fef767921daea76931972e9f6af5c", "0xab9652783d3e11b0d1331b823d2d49dab8bb13365e43cdabac7b780afd671200b01faeaae0a55a4fd0db21d1026c8d61", "0xa1db51e9944806f65650886f0b3a671f5a28dc5f8419dbeec0d96753a04dee489c63ce1f5cae85815299c496e1de0c40", "0xaa33ec9addf8afe943c48a698acead94fb882c5d396464c9154913d4522cd44428f68fc28e06e81a8800bf08ad5af0f1", "0xaa3b365af0b076c34f20faa87009bdc368a6e0f4eb9bdb3a842a4615b4381e860d65e80fde953c8365708b84fc768c37", "0x989be812d48924493e4bd1c532f2fa6d3c14da5ed4de10709f8045780bcfde48ddffc631ac005c4954889f0238062261", "0x869f1129342a58b85c997a58e6649db82dc90d420c78fec384ba6fec72991ec16bce2cc33f6331f15f60821aba87b67c", "0x88ece5304c58a6bdfeb1d5906c14ad7f62e43fbfb25d0226ffd3717d0bfd4ee9bbcff47c8ef31636fa29698e60896f3b", "0xb393c2dfdf1c7a42e4ec2ddbb867a8d100363da6effea717561d47734bbc0f796930bd3f388d15bebc3b80f5e1ab7002", "0xa58a7ebc53f420be2cf6875a8d8a84ce0cfb8dcb9308403928011bb1a19de68a1a788d534279394e2491abc4f2ebef53", "0x9395e3a4901813197b8488685f0d422dc210559f2f12396814e1efe295e1f619c3da91f2287476a76ee6a92cf60a74f6", "0xb3f76e1575e3bda7a4ec08721d0dc8087afa1e1165c69f471a5d9310337cb654795f3d343883cdda926022328d1977a5", "0x9636852a56ea9eb45f353519ebc0221fefaa59c6cb07864a6b6098bbcba2f86e09083a611d66389f7ee89ecb272cf089", "0x80546bc1fca3f322d28bde5582a10e5f202129bc6f0c7c872f228cc512c107ce49f4b19b5344744c95e9dbb38e21c110", "0x92118a9237b9c9930d5681c8f596058bd624f912b19f7bf8bbe868b4c5963bf13cbfd19381aaf31e8fe63d632150cff0", "0xb511230a2cca4a007650fa73098aa96ecbbfe37c7d1fc52920731b3b2ac0f23fb4eb5550f33cb682dd1c135ddf74edb7", "0x856b85702a86d006e160b962b7718035bb1abd5169a6dfb5e52ca1696533d0f00e79ebdd499b1bb616c85a677ab3660f", "0xab7e4f74b29ad3b6259fb9836d93e834142a43c745b71e2aa8f2fbab72c21ea8a2e1676ba8c020615764e086ffa55907", "0xadd04e7df484a8957d431708b96b3a3849517b289e3d6cb927cd4b36f5d5817227d2124511f1417ec96026c223030262", "0xaf9f833045ae702f020ae896840dbffb69de620b45c4d8f03e1f76ac10547df92ba8d4e2adc160b302138b9ea8774f7e", "0x8f1e6e8a09e12de6c37784397cbfc34c61c3affce034625e8157c78e960b21ebb29866709f1f174fd8b4120ef97c2c9f", "0x83a2a27237eae21c7399bef4d76f2c66005f7461498a4c3da58026f2dce0601cc7f9438ca3717349832e303d3049e93c", "0x850723e4435ee7bc8bc42c84ece631566484493f369a9bcb0cfda9871c7ede6b62f605a5b9681ecdf90bedea512b3a89", "0x974fe7e61968989e20ae008d52cb50aa9178be13e6ed034bd077a463335b991561772917730b78c22004f1cdcdae6c7c", "0xb781fc997bfc3c6c87e233df3e27140688e0413fd58bd87f948aaf38d6afead68b44ed422028d9830d07436db5d21b84", "0xb970917e94823dbdfe46fd739bbf6df55651c21833bc644bcd6a70567d6c0e189f558caa8074be73959cda96c6420d68", "0x93e4c30e29c30ed8432041df6e429766909f524bb4c748710535bdee2203b5445399927fc1f40eabf818385fa70a15f4", "0x8ea725adf5aac562b9165f92825b34b5e2b6e4b2f01d504b70ba9f469f459b3b8af2c5fd1726fd1e20d2511a0cd4c80d", "0xadd51ed3ac56d03d9afcf55174868570d817736ec41829e0245fbd770212c8e8ee07a7ccdf5f512dd80fde33ea94034c", "0x852fcfdbf4b972f7c646cf2e59ea4632e5fd4fff79c93071e399293916be684841bb90eb90895ef3a8dac5ca970c2567", "0xaf5591a43256e6369dda3e3f7bf5b8c78517bebb8ffcd67970f6dfd1652d4c5620c98dd861c71064cf102b4712ecb6f3", "0xa093421d1f790242c342d8ace432f92215279c8a1e1a1779f35e70155f15a7c111be792ad5a58e94ac4d652f2aa1b5aa", "0xa557dbd0fcac0faa69437a2a713e445f17b957c1bd692d162c264c453f101861a2ecca80fedea2dcc0a58848a4bc1892", "0xab869f9b60d7c62a657211fc3f6414c7847405f5df6c596fe919c6290d9413d343c3e185fd34a12e0b239ad0855ac09b", "0xb57a67481f370bdfbfcfc0eba9769b140cbd55845daec962d7dbbe847641debc596e9c4073d56323adfe18e1d755538b", "0x8b453c107a57d1bcebb5db5f0485d1c8149dc18a601c2d749ba5dbfc744271b30fecd2af487c75b42145c0f726238b7b", "0x83961dbe29f512af8116f1d82f83324e96a7d92d665e7bf240e37894a29aaf483dea898564a8fe46428e6e991930e823", "0xb375bf5d0aaa31463caf8cd707ab668df32e76001be85ede01835c8f23f239309b4981751fce934457d9748edc36604e", "0xb0089b4c07a89b958b2f94383217c76cbfcebe65df98a2975355ee5d4deec9dc296305f0b2ebe1fcf48637f0d9b13236", "0x982db3442e382b0536a5f2be4a55e2350cd8857e9c67a8dc06ff0c27265fc174d6e9c9435c7d957cf5d61d31def49170", "0x9220b030cd10df9d1d23fe6171852ec03b8c99f1ce4138269d118f5f6935e4bbbc0c45ac10b61b798d38af8d482df1bb", "0xa4dd77a43d237ac41e4921ce2dd84fed64c156bd73dcbe858b54c2cb9c8a6d92453a4c21aacb615e18af77eaa922a896", "0x87a2bde9eba7845bd6a9ef64fa8565c2cbc8a60982bec973e1f4cf8326b5f1f76bbac704daf897a40de28775747badd1", "0xb6258707cd35dd413d54ac3bf0b9073fa94cf9f7d7aae876b09c4a5264357a9894fe0edd80908144a5a2c5afc20428e6", "0x828b2a294da7cf2b2ab49ece90a0f2069cc5682d15a653a76c6e81217d798d64be0abcc9314d272ed931c61b3c922034", "0xb21e385cf54559d9372d99b3fcb8c7cc6d1d3195bdbee30bd080cdd800f426cbdf22e9bc802e28d5349f7d02236d03e7", "0x98a70e7f7596bf6ce502befb31349edeae390fb14385fcb9ef8afffd6b5c21aef4cbc5049435cab565ae32d95b59be26", "0xb8ffadd44e431fcace7ca7811c6669577e92f4242781036897505e9e01a24a532d01a1c0c26dc77efebb05b18f6ec489", "0x99f5e24d0389fd6e7ea05a6c4ee6a0ad002faca1699ecd2288a3ad7f05b2fdd61dd314e9827ccbc3577c0b9c696addaf", "0xb2356d5975a0bcd91cded7b09e16e1f2651d5796d256be546302643be48f237ff184c880ee1d1a05b39568500cc082c9", "0x95a9800e2d6aaf06353e581cf9ad5d3aea8b1db2b3143ab1385e7015c02647efec707d466e338d553dd3a1417b0bf2cb", "0xad2011badbf6978fb4b7f30001efafca14d0e44fcfa322945882f76308f3bd9a4ddba49ed6c02f727d5e7cee7edaf029", "0x86dbc1242d07a29a06a0fa2cf9ca4cdc1999a2cbe74a65edc279755aef7ca243aae0cac8508ab6da6d4dad9bb5537e78", "0x8be72fbc8a46bc9248b75b7b3cc91d37cecd1d39d30f40d378ae06281f69df1d2a6a68b62c84e027bd6a679b30edb3e5", "0xb89e8aa1c0e2a904b596953b2be14a3f2351178a59ca70c9b639137ec3a2f04fcf14167f979d3c9fded98e7c7a788c4f", "0xa76d6161a74e9dc1dd7ebf20f27d9c8a0c1cc1107da5175f6ae3124042566def94ce345e45ea8656024e7d07d608467d", "0xa447efd002de5d903c02f6e4127cf44004dad716aafe1d4dfe8b6fa9f742d3eb2940f5d24c4b7b0f2c9918b74df918f6", "0x8c97cf0a8accf0a1f1a69e46b311196f41aa5d07840ea539c0a4919be295ffb7095999fc83d413c628b73f896a6357da", "0xb3f02251f3b4659a996d810832fa76761ed00a2da7b5289ffaf42c38ceaf5d87e634b2ce600f1ee3f95ff402c180734b", "0xb33c07c7c6bee9d7562bd05164afa8b31cd777bf294abf500be0d9196f9d9da4e9c3c4adeab30ff0dc500c386bb44f5c", "0x907d46892072b6f7feb5320ecf57afb8bf7b407d9cbcaf07867ef3341fefcdecc924b87589e5661e7b51303d094b7182", "0xb7bc97c01ca954b0f81c3903d6f4fc3cbcad5f7621ccaf809e4680ea7f634a50be6d8a8f9dbf61ea9c794844a83500b8", "0xa21020ec607c5f31b89a3f5336ec9bc46ae6f2a995b8a4584bc8edd72850493f4204cacb4707b6e28d0ebc110b69f2d6", "0x8ab6b7f3e8f63568f9c441329471137d33c0fed753b098ad43eb54e66667b4f77493911e7684aa7e7e852f3082827238", "0xa22f47a973a80d932239c04cdfeb42cf52859d3e761111197cb35368da8afcd56c4897a47da2c1a4f71fb93adf08c9b8", "0x83943fb854dc14f53d77c5020459ae1a1b1d9263a741eb03dbadba102abb2da868a06723ebb6cb0d198bdbb980ff8f28", "0x91c9bc5202f9af59fa47703cb66cbaa4edc74538de1216032d6be051beaa6e4f7a36f33354d7abb820f2c49d9dc372f0", "0x8c1f504aab4335b685f0d9ff230887b18d503df3f9d8fb51188285888fcf37df4221ab6fea6e4f6a777f37f3a4238e90", "0xaa2c13d672f35479b8317d5dcbd4193592b6fe9120e4034ba9d1f76d510d409413660517849c5739485a2f9f38ddbc51", "0x90e10d58dd4f8af4f3c6c7c900c3dbc182fc8e72d25f59993bbb85d116c259d43576aa43997637dffdced8e9f3d77f23", "0x83692f6590015e7d3bdba0abab90eea2dfd9dbd08bdba3289a1b87234cabb6ef0e95e1728a04d2656a1cbad13b471d9b", "0x8601c36713636f8077c8b1ea82f8549eedd788d7730aff0b2bbfe7b8573dbc274c6766c8d4d298896772dd9f5f8bfb09", "0xb96f51bbc8666817b6f3d05aca2d41c43f6f298f427e0bfdad471cc4559efd52811895168f70df92a93cd554e8c60f0f", "0x8c58504e12b206f6e3970de5db3c516d8d78797960a1139b0cfe21735e8f6fb64b9a40552c212d329c0960cdfcdcb3fc", "0x90ec5a9543d7109702ffbb65c64162a69b8c316c4fdcc43698fda382803678c34f1aae4c43e785b2e6b2e2d1bec30ba1", "0xb3352f2633a6dd80214da1ee5717eca609db9e93703e20ebeb53d91f0d6419078c10df8d5f83fbe5addf0fb217dc41c9", "0x88677d5fc71a1e3733ba45dd4bdbdcea01ed23727eb21afccfdc069c97088b57af9ee9110b774fb32bb4cfd74a96a029", "0xb30ff2d16cd79a01040a514faaaebd32f7fcf520872251d39b2b2543af5c12189a85abf31b303a83129e2e8b6305e24c", "0x9542e1ab19a3150b7eb80ecc5d01e67e8262eef4323436ea63d0217d2773226bddcd6b575a9cdf043044096a32e360e3", "0x8b2f2941998122750a03653c96557762d0ffd56488d89358f14990ac46c0242ed6ddd5dbde67608ef6f2ef6579efc251", "0x96c99829d3de92fcd5ac28f20383288d6d37079e44c05c1b0c338c47bca8b1a5bfd21f6840a0501c61b5c32b3ad9a3e1", "0xa4334da1ad7c8d42e1b3f732074032ed2f7c72ca8b8e4af93462015fbfe15e90fcdb9b65a4bc4c52cace8744c169994b", "0xac7f825bc36466c7f5a769a4827c77e6cf3f9f863441a076e210e40432eada690f349452c0af074ea781af613e6a5967", "0x8beea02765119afd5e78dca720b07f8c539c80a2109b32039cae1aa82030b84ef727ed3e65cab4a9c55924b77c79f59e", "0xa0f8d775a6b40a55b32b634780524927ec38071e629ac1d84383ffef82e37f89790a131bd99599c022d9a9f4efe9460b", "0x8288996173e30affeebb92a34a3b828b272e3ab9ed7634aee5ced1c802a0824fa1a3259b9172bfa34de2454efae6d04d", "0xa30a05b0f3ad9e336d92e86278ad9142a648b0f2d52681b8bcf512f5d0d5cba3248b6e6cb1b713fc079b0fe05d920820", "0x974e728d0a989cb0c2e6bc3257dd9e88b6962bf744c96383a4a1fca4f55ffecee8e9261699e2d2af506adf59ccfd4f39", "0xab8a865184c1d21ea1f07a0a690e09bac0a297798adcdbd7c920622e8a6c904bc0240b0abc9ad46f8f1e0295184a78fe", "0xa86fcc218e0f257fb564ca42afbfc48ec3b470620721414ac5a1e71bcc7ba7f26b8f8c19f8f8a4cf90d1cd40434b6f32", "0x9330f4d095298bced94091115147493e17ce9ff9d9ed25e4fea63e178a7b4e2b1b30b2cfd5401621c7eee5311ae7f746", "0x871005049dd3ae01262efd20fee38ecd097a2044e7ac762c9a67842c8600d83e4da994574d513e5afb1c5c5f8d7cc188", "0x841f750df7f59bb6333d629f3a9ef2e161c0c21672ac22b95df05b871fa8ba545d3d5cf30fa397954d053a1106146c6b", "0xb6e15e573d7a58ce0299271c56f66de208e318e63b10de11bb929cfad32cbd65773d66193c1824cd7726317d4e2102bf", "0x8e1d17e7f1c1d6cddfd321b29f6d08b3f1218b2213976e56ce1a697d48d164656da59276488aa6aab1d634c31c1a56a9", "0xa8614ea4bad1a2fce26c5b5705a1769f48af89e78959937639b7a6bafee17e9238c3b0ce358ccce2eb20078dbcf37bbe", "0x928ab7188daec09e9eddea7211121100a3e406b6e843fa452ee49d562754a5a6f7f3198a545f71e1e2af0cfe95525ee5", "0x90d2afe5745f5ebb7389e15f16c3827ad1e215d3e932f4b18194a1f7699b884b6de27b51ffc7ad0feb5a372a621c7b7e", "0x85ed969abb43d9f4966fb8342c00c1ff70748d8dd41b86f81ca83d66311f66dd00554360524bb82ced40a45b2dcfa833", "0xb021909bf26c6e4e073be8f07743f9c616e9a480cd9af737d37915bec5564ded90a3628c4b19a44789867b457372ad41", "0x93227d66fd02aba4f1b10c5ebaa0673bb80f1aad458cdcc3fb50a220e41d0aefd46562ed15f8c1cd255503bd9502bf1a", "0x828a62a8122215a040377f1518afda12e2eec7fd7441f956aad35f277e8a1c7b07661573d060872dbb6b918089672e24", "0xab1686a0dadea72d6e89c1fef28e7ebdc0462dfe24ae267e9e0130a46e2d16d79ef386fdfa6e9814329229faeb1ae55d", "0xa1fab256c2cfd32db9f0112466a059868d5a99a5a40f24f0ca901f87795998434e208ae0c5fed1efb93ba218701e8ce2", "0xa8936189a64f1aa1cd0246b66e15912701fd8f451fd6db22b87dfc8ddbd4d758f65f629ffb1761ce06acf54444e9c6f0", "0xa83061e92474e725a30417022f82cf64a10540f41c9fcea920c4f56dba6133c373004508c70331155a1e58a1963dfb84", "0x8a98fc4268d000e62116d9849a8bc087afc166fc0814847d974bbdcf21737dac6b932e210fc5a12b1da1ab47472808e4", "0x92f3aae3a3c2aaaa3bc8d6a2a6fea766b20c3fc035d179571376365785513a00598ffffe8dd788dd20af038758e4e539", "0x9474c1c2defa8217a522541ded9dca100f0a254cbada905dc6a2555955578b1668d46e35ee0220dca067a9f36e29bd23", "0x89437b446509a993e5fbfbb7e4c7d2123003b40c97ca822dab96575b5009a8fce268655e042f126df77d72f15105a0bd", "0xa2b4d916be4681ebcd81604880ec9def10f7c7518956b8f7749c678b03b98c0cd20b5828c3c433027d2ae58f8270be45", "0x8a125961407cf45c1a3370b45f330e2b0f8cd4beaaefc51ef4d746a6fd23e06683d0dde2c66543db56d3177bd4a6b095", "0xac020994c6fd3931725b29b03d84f64583b41957101285f416f25f6e5514af2d95a6d5d526413177299fcbd2e429759f", "0x983100ab446a94ccdf5f770d2b2f85faa5f05488cca6d896dc423e0982eab887a5b00f1a5885af3c4b312e7da4f78908", "0xa6d99f5a77f8e91dcc424f0dd66c6bbed9910953d17fe589553133a4099ba4905455b290ccc262a7b833e9d077cc9bde", "0xb398ffb7d7d93bc5367e8dd87eae5f3e30d5c312397250f4f1d999ec5b6c27129a9efcb9c2261b6d02591d1b29b9fe1f", "0xa30ee5d21e43fa8eb9a1d8ee76efb5771eb33402c6fc139ab4c2dd711f0eb915b79f7bc5b1362588e0c4c34059943727", "0x8f8ad406b7a6916665aa4698dc0a10e5c0beed66e2a5c79086e145c37d0c6a5649da2f494355bed9d3bf8027ba89e860", "0xa02927ba5d856f6bd6d1ec98e1373793b174aace9e44397fb0b2ead1402d03b2ebd6f180a3d89d9a56e567eaf8617c7f", "0x8daedc8a171968f79e1837ac1d539077c73948a076a00017301e1646e2d12e495bd0bf27db3e82f6e6c5340742d25563", "0x8a8aed173f3419f840a68d7220b2dec78de4f44a58ce268678011d97f5adecc4e857f4454888df73949e20640563c52a", "0x8b7643f82cb0af99a4a8cb8843f895943a5c33fa2a79074f99be74bb19ac7ab027c1acb34dc62d5d2075f525555090e7", "0xb18989bfd0510c9d2e1b574fdaac36bff2c923441331b7215ba26a7c7b9dc12ae92e979a2830f020ab4246a55aabd372", "0xaf7f3e0391714300890d6d4b101c147f3fde8daff4398e2af8fc86a0b209d2039134464d10bd4441a2cfc83f2794f952", "0x8bd85affb77c52acc49180091f62ecb438a0927a813ad27061bc7728a00d1096c562ad32f0c4a5977c0958ef944fa474", "0xa1f096ccafbbcbd95f5eb51489a219a1c4be4e43e65e8e72aae9bc97fa0caff2adfdd89cf24768f53fb14d0fd596ee47", "0x932398be524652f508fa3de913f1b27a109514aa1e9084c9716d6e8ab245042481aef26f160854740738ce25820c8c4f", "0xb71b092c6d036110d45dce42c5fbdce09e56d5fa22a8403a07bdb62cc3d6109b2e633364d32812eb566405b553a18361", "0x985b02d50b387723a2e36531dad8e26d9a2a2a4723f29fe266abd56ebfdb24fe9a866e7e661ad2ca2901093934e4335c", "0xb3d0d16a552a19615d0a2ff2003b1f5efca9206e7cfcca6cbad1a11e037a1783cc2c43ce48c0a2c8e97ae48012d5871f", "0x8f9da57c160fabc85106efab6d1cf586b23af668fb80f6c4a064148e7d451c57ad2fe33af9b1de97cf66d40301f4e08a", "0xa567fb8d0b15656c647a110de44539e072ff751a3202c79d370de8ff3a0ffab877d6fd7efeb8cbda7fb35522a3b8548c", "0xb0308b8f70ea8f12471a4844f4f406ca4f09c62cb2baa2c2780c5a8ed82679c72e5f18fffd820e788639b1c19e5f187d", "0x95e648986a345d3e3bb8d50778aa342e17932c5b3c0b77cbcf148230482cc3e8ffeb74dd5ccb73e2237466b43570511c", "0xa5269bcf9a7a27943121696892b64033581b2c94fddba0ea24db5c528fb16a14eb1078db5baf0b591dbf41de01a792c1", "0xb6532acdfd64c8d5e1ca9f8ac86d643d9d74c042603e03b74fb7d0256b99458b22ec09b9662103d24f0dfc9fc320cd8b", "0xae52d215d1603c189bf1eabd89e7c5b5f15f053d6a6b927c9d7579ab0f14d0cfc4960f304f98ab1ed9053e99a63bfaab", "0x82c42a63a384e0020d7d92643a11cfe96c79f097812c3610a23f43866f8a99d7704a951859166c6cf5c2f3896344ade7", "0x8606cc26d192f16e18a1849253387ecd96451cfd495cf95bddf4a15283c1a23ea8268680b0dc00febba0596e2246adbe", "0xa66430c9da1f7e1660388d2ceb8eac129f563a971261ff3a6300366dc51f8bcc012f9791891014cf6714f5d1c6a8b87a", "0x9765a6ad2ef774e33d0458639e330086deebe84fc36a94691eaf3dab19675032157b04043da442d8f87d25b75a9aa41a", "0x87fa672d513421d4e0ab25a4bf85aa83ce451261bed2a9ad6fce33c1a525b71ecdc46fd1a850d5460464b8824fd459cb", "0xb704128d28215dedcccc5a315038a7c5bbda85b5c9ba0c2ba879dcb8244287762640f1b942088b7ecd4742d88e4e055b", "0xad170e8de7cefb250ab2d0d25ffb1f395f401d5ad810e6092b58af61984d3c25415b428829529b149bf66ef8349c0030", "0x90881d254b1d2e15744467340b866bc957eaeb61be37faf239bb3c4082a2c9d729bfbccffedfabd57f01403650805e8a", "0x8462a901bd830feafcacfe390a15da2a9705ab8c829384a14914debcf96b63194971d24d26f5c16faf1b777248af1cfe", "0x8f9451000196ddebf1f9acc86c05b901156803b60880797c7053d7b140625df698ed03c67f72df5fcbf9092c2322a124", "0xa568916ad0ae7a12bf737777225e1918b7757c1ec483080b074fa7bf10164c992e202cbdfaf7b58fccb58345ea525c12", "0x85cd81951cd26ed100e66762a3a68f4046c07a8bfcf507a83f64cb6c8edd9d136fc0316bdaa65a7f65bee6f5480d23db", "0xb654061392124d36f1d29c44c833c98aba0fc27dcd98f75293f59a9329868eec868a3d2fe4cd115606fc9727e9eeba3b", "0xb18d7ba00d85502b28e38e4c0a57f373e0daf2ec0becbd284b839d7a18048d13943732da4e4972d2d65d708e3b4c1b27", "0x982d111e6be22f94ad3b4b60bd06c6164f4d318c3dde943997b63483a5155c93409ac762d59bd3c073ed5b9f2cdb9798", "0xaab0f191119f0bbccd6316a1761817b312245fd712b5ceb115acf8f86b7f82c70fd24cd0ac0a8617ebea09f96ba528d7", "0x80203e058cc7918dcd7a8821664f1ec2822bea4f6eb6da9a79bb3339f576b27d1cf5741f4adf2d3a480a2ac30bdabb27", "0x918a39ed5d10b39fba0306ba46f82c73c97e5c506219aa103da5cc87a48b64ffed3617430dedb6204efbe813b18ee208", "0x8b99aeec91caba008a695a5ea4b58bf2079fc76b164c1717f60136165deb990291a0c8eb85e9e94c97c8f1610f5993a7", "0xb0dcc4ff5acd777cc37cc5818f30cf6c7428b68b1443f3676338ab23947a8f4238476bd8d7be07a60847b75bd247ecd4", "0x865630345abe182957658268d1098c5258c39bcb070ef6b3c899889bdd111cdf403f8b7a206e88a48ae10b15934b3463", "0xaed0fdd55f478802df40d5b1e1441baf756e46890e821bef2476f4ef2e8c9b1b2fb708920911aa47fbe6b381f350a204", "0x98991282f2a9bc10444c8a0eb77c6109e810f4410aa7aaa297d710ed2e0043689ebd26cc589ee2d7d7062743f820e8c8", "0x9930d1ead84439ff3bc0e6f22564cd79eb2d551ac1ec0cf469a7ab721648f1ea4d3229f819a36b5fa4daa5ed27497956", "0x8589d74494119a1fa26849e964c4dde8f301d71cf68add219e872566b7d81f683f78a019ef704412c66deadf436937ca", "0x982ef649dc93258f60a649435f1449523c62d86761d86266f288f2cf6a4cbe395310cfb86098985d565cb311fc9c4d2d", "0x930cb82f4522005d737e2db4225357fab144fb9f00e3c6d383a20a8dbab6045de03f0aa9669be5ed7366ec494cdb11c5", "0xa99b1dcd7d112cbca114c054522083278de6814e8ac420fa2d6bbc0127ba43c3b6aa12c1f8d9ccfd8761c050312c1839", "0xab3c7c79d35478d6b7ccc0514bc893a8c88dfd10557594cf4b696cc42b4f1be0f1a535b9ee6706fe25a8f5b268ff3c35", "0xa88b8795b2c3d88fd1c1699be72e50279d16723b0af48767e30264d3a74267994c099054a313d65c25bcff6fa4e2382a", "0x99c77133b25a6543001e678fb67390115ee439903ae1f824a495539c988364bbc0981b9dcdb27578e0c22800ff6561f2", "0x8268fe6031fe8fb7322f90fdcc4ac0e30ec9dfda371d2adefcd7da3e3613cfe21f0a9130d44e741df16c0e20ec33d082", "0xa996305ff9e1268924a61456b0b9737dc8d5d72039ed8ed64ec8bb85f323f58653e0d9a95ed83361bf80ae1063a0f75d", "0x886c9a8534409548bc178654607a5908c0c1e78198bb5394ec4d3afd16cbd06af5599dc7134091cf09d6b5d9c4fcfe57", "0x96bb7842c27c49c0db1a74aef1d17058290f987ff57d314a384cd64c145b98770dae1467ba17ac71343a32e5f5b06fe3", "0x99c626062bdcc6bb6726b653adcb5242da3631341e72b3020943e06c78069f64bc9c5597c526c5e5db2d15d300a64ff6", "0xb73cfcb5ba32d19856e94b4f687ab0249a5c5a801265d6365dfc1b632b7e8a7640d8c771c6dbb89350999c5b80818373", "0xb9a1dc01ac8a9f1010c0421e6b0a01f44df7320e8e2943fcc97662261003c898fc9b43e0efea622966af01446cd48c58", "0x8ea23ee5d27fea1bad223c1233f561ca30848b600af4e64f37ca626eed14f33740b466aa3eca9bf02cffb191d24489e7", "0x95db6001439db1e4679f126fbbeb62c90b0d10f84a4aa258b450991862ce67ed2bb87594a430a8f304adc3c40ad0bf6d", "0xb372ae30a959e6acf9e4c527ff37b38b07031c7bbaa569635c178eed30251f6338b72c8c936af5b2293445d21f4d26d0", "0xa597d0ae91ad5a670e0e77150688b22bcc67a789fc77e7307b221b476e3d335f329171fe2ff55a071cbb016093cdc163", "0xb61e7e48e9f6dd0f72e0f7d9ad71a7d743a10df8e4ca1295bc35d317c240ec027e7ec72321b1b32270087357c87ad5cd", "0x8b7db9f909b8243127d19a7f20fadd7b30150470b5ea6f4152c12994f651901dc38b4c0628168e01edbb65828bbdbe64", "0xaac2f0ef55d5c2f77bf71d793fddc7662117ae25c6baf4e36fac33279255b6e426540660950c2ab08492d1ebb45078c5", "0x87f732cc8c8923a8d0ca515bd12fba505f072a2d43e01e7c420e9edf710b276ea57a407cc821624fa8c09af98ab07f51", "0x93989ed93fd09fc0b286a1159cfedef259a4edd0cd6bdc2399349e0d78dc802c7576d503d43f1102c629804e0d4bf5cb", "0xaafe9cf7206a70c79ff9148b1e3922fde92ee13033f22bcfe92632b08eb552a393c3a322d98fbd71b73edfdf6a903bb6", "0x804b135ce8c0b0792374cfd31b78c3b3274808fac153edbdba089875362db7be24d3914b1be071a00255dce0c128d222", "0xa9c4a99298d974ed9ae0282f70b5d3f7d1a913e33995b90ae055a8468a5d6f0081f7db20fc8ebebb8942e4a375afca38", "0xb2c68ec35bad4dfe85ff73b1df1584d3c99ca62f44bde2d01b4a1a20ed9e8309042005d373d3b93bfdc4ac7899a2f4fa", "0xb05af0911e0384a7b55ccf57689f9f7a2e8a85714ee8dbc77de583a0203ab96389eb623a5198d6655ca12475fe8abb3c", "0xa8a77f72cc15ad43f3e7d916cf04b6ecedbfe67a6d885417ac0adb20412f4ee5b71afc9ac68c6dce3fee7693efc080cf", "0x8f79292d2fc0bdedd7fec88a0a7d6ad27338d8455c06b3a7645e1a61dc8bb84c72302325b7d8477716cf29959bd1683d", "0xa3c7dcd584562a48e9e7459249f50b9683e40278c691a6f16566d23d009f2a7cafd7f43081817108e36cc654b23ee576", "0x866b9dd395f464675f2ee7d302d5c92e3622dca4cf5083bec339f379221865bf37874d57e2185f0a02577b04fc7a3633", "0x855e13f900034852980d2ddc4f9ace3282d498fc764eeae91a9ed9628d2318517a828dfbc88eb6aba2092c94c69a37ca", "0xa5592cdde2f266fb3622d82c7facf0aae50079e4254817882c5934f77488ffae18719299f2b25f7978995e5e96a8c417", "0x876d4cb1edfebb5f1952f0c78ffdd67fdcff367f63af7ffb14b8c780247e3024959db1e256fc0319f55a58fc470d1611", "0xb35ec64f11f98858c8245c7a425f2235ed95835163bab651ba87f596efcdcdeb481d63175b087048cfdb063b967f8440", "0x816dccdfda52151e4fdd12974f6dc3aefd2ad99e55eb05d3325e6f1b5c5b199068b80d7fc4e7ef338db3105814a20517", "0xa81b92500755153f3583b6d73b925b1c2fb0630f46956705203eb1dde17a2cf99b575e0510c9049ce0f437143f3c2d81", "0xa76efa3ac60af3561369e200dabed25eb3d3fac02cf99cae2910b89f1dc1eba3ef47b2554c313adab7b16580d36d2a42", "0x912363ff5730d41472f6dd58c9cdf42f5a37d02b56527d3e2ef34c94e8744ddde87bdbf217e789121e6261bb9b8f57f5", "0xb896d69233ba617ea5b657a960bfda1b92672d4c631b9431f9dd34cdc15ccb8d31f2f7107e94827516ef94079ae43d75", "0xb2ea369841c64ffc5d06bf6d35c2e49493d9fa66ea1d9823359bdb4752557e0f2ca2425722df0ff20920924c7e22e318", "0xa5afc6634bf2451e0fa1395f25911bfd6380cf147e3c56b2c83ed60e8769d7717caa6a49c4882749d80663a52ebf3ee2", "0x802dea5290cd8e289a5115a86c3eab3b53a2963069e20fccd13ff42ce12d02a3bfd02bc0fb5fe90ed82721401058006c", "0xb4e8d58afaecb4a609dd62f5107ca206cc309e47d483994178c9cbe6ec8d3f8412e72977b71cc74d3a976235ec948b1c", "0x971a4342d10cd716957cef71c67be6b0a77511d9143d0f240a03de13ca0b1fa3e18e219b16c5d15a9823f6741cb9812e", "0xaf31a08b82bf435051de214ee864bf571c086a23d1a672ca4c80ae50d4df05291553b222b5dd436f0959c9e654205678", "0x94839f5b67f092d4f2bb265d6c3d527848e71e0fcdf52c2a5bc6ab265af33b983dd2729055abe254e8c7efc195ed9c49", "0x87b423433fabdf2610d98c8ee8828a12a744e66efff602bdd52a581dca08a2bc7370fdb81585066b5add1c5455678054", "0xb7a6fb59679ad2a9cae48bccf24d06d1696c37c1d8b64165d21148f3a08fa202f3162b590816eccccc2552637744e461", "0xb667df7bcbd84feb0eea40a8a2975c90c4f655a3903e5f1e851bddfc2d27f98da64ea9f6c62f5dbd969606222dcc73e8", "0x96cd3008efff8e6e3f9498524719fbd92c8e07393308404781b6a33b17a4914720e88fef80da781c0ca84076fde6c25e", "0x8360308e7c5d0f23bb5d3f5764174c789754293ddd766d14fb5460d23010ead4855909fb9d9f1a665240c218951f9d3d", "0xb884c6f1d0e735679d1374dc8760f7c74450000e2508ab4954945eb73272a253ec6af09a3103f5a27fa37b5cbfb750eb", "0x81a44bce043a47621a62dc562d03dbf6903f67c52ccd55b92a3d85f5d7cb6da2e64b21471a326f1fd56b05065edd8475", "0xb99fa4084575c1be48621c5537b5524fe97ebe2e0b9fc61f47726e4b8b90b6a9c2e367a04c33f9700a9214e3385c4d77", "0xad794f63ccdd34bb9d8d9b3153d7bf83dece215c62d3df39821269f96400e5403d267ab9fe7ed6178ac1bf4f2f066b47", "0xb6ba7afb558dc9b481cd6a10b85dc32d19295ecb5ef6437b8e2f29680dd065d86bbc210bdbff42811162cf95a77529b6", "0x933a998620657ec47a1e8e9e962cb31daa9837d368d71c7cddc05e5272e2739297697e6d6b3600fa0861722a4ef5fca8", "0x963024910cee4641ec168fff66e4a6c558c4e538338c77ca574d76052605f5e6fc9e3daa1c47b1bd0ff2996a2cf53f45", "0xa7201ae0c33ee769f857c3f8a67f6bae23956ccd5e94fc38aa6e7104da0e29f2043a2edb8500176858a8bc0dcdfdcf1d", "0xa4f86374656432d427f521ee41eeed41161d02701b5a667f9afecf52015c25877ffe6edf207d4d8acc9fcd366fce5282", "0xaeed8c4b7f17dc867341445fa21c83ed25a8c9c9a8f43050a4cba6b6b2dd1f066110e2356a2e99dac7ee8ffc0e78a318", "0x84e49a66b35d27406bbc160a4517bdc1dbf63072bfdf78a8be63fd9f8770f893413c3fc4bbe4ac1ea7852b900bf40eeb", "0xa8cfcfb9baa03caa9910df2c65ffc02198a3a2654629014fbbc5f0bcdb9efb3cdf686aa4ecaf9c68a80a73fd821d5d46", "0x93d7bf0440df9e8e406039f610833ac9bf2cdbfc06f76525a38950beae923ef40734622862a7dba61e90c8ace06f0ead", "0xb4e1e08dab625d0d15ea7b780c1ff05ae290aa06842b35b4af44e5978027094f2207db329d5bf67c11a2ba60ae7cb505", "0x8107685e41b2eca5d081dc30b3215700bb46f5c32425e09f571b8c196518f35c300d9ddd8dea9cedd72e71f20f2a7d55", "0xad338f4321cbc7162b159fa8e0c68e7b94c92c72dfb1bfd05476729aa944c0200e05ce8468ab822a0dfe18b4219b2cd2", "0x829dbb3a4a13de16227ad16d62f820bcedd88aba5368bf9fc4f6206f186572898c0fe88509d87ec32f18108490f7404d", "0xb890b37f8bd1a50e091a2bd7ff63b62fa38ff3012482f8288ed1f5b90c3994369257b55a23836c0a870f615e98bd1d79", "0x988c5901664fb4bc09492003ba61b41ec7e833246c1ef26ca6024e7246bec726e8f3182a29d903544dd24bf9e38942e6", "0xa95ce7fc51a7832f8dc94e710e8d691be873171ce3871d557b95eeaabbd094b208b95c82e41b7e1779ad75161efdc794", "0xaa72b77493d7ceaad849c3cd2a00c0b6fb0dfd47d82bc8066a44ca11694d233d0f2246902e9a74e427012fc0b3cfed80", "0x918221fa1b76ddcccc1e93c0806b92f8b0d4319a4fdec7d6c078a47cbd0cdb96fc049ce541b8b4ffd1a62b773f7804b9", "0xa7eef609e37d6ee926fadac17f230a1064a3eadaf41ef997cb19bdc2641593ca4f8c691b4bf49c6816b121731e86e890", "0xa0899605b02fde3cfb6b1d938aab7f2cebdee6b4bb3691e75e52037dd6a7421a453136365c82df959ef2065cadc28cd2", "0xb1a8013c1059eb995ea7ff0faca2eebedc7ed36af6dc56c55b7b0df8d2e46bee807d8ba006880caa75b0d06f4554def7", "0x8f1b63cf28e214612c2a13fb0b0fe77be00808b53e364a98c636fd9bae11b3a0ddff4569ad93f976124eb32a732829e3", "0x93aaa645b8d4907751c7b38c09fb89efa2713043e0b1102dffb676c05cb99283a9c86b1ad5da1c6f3b7d0f44409b738a", "0xb4320c83053aa80956df5056d44c385865137a1d2c19125a6291e28e84fab4b4797b68dcb0407935b29ad67dde8e3ce4", "0xb33901e8175c9816841d27f0ae7876ec18ea279d565af8b16bdde5005cdcac7c4609b80d959ebac1dc4de91b5e0edb89", "0x90e88127d8d4d99325f284d23a310fbb89cf8233a068b7b182f962736409b6c56f92e47df9c2c9b01492e637165291f8", "0x95db98816ce525790bb1277aa55bc3125f31047ddbb254c106e65632b76b593d9adc9c1b8246761d098d91c766a8b2fe", "0x900b4c7e14f4b2fff900e8eb8fc3228f882e5c3befcd2b25e185ccc2ee9fc5a325b91ebe55b80adcb56dc57876f59c3d", "0xa8377e7a340d0597e857bd783cbba2c0dc06a00d8961acb7f9a640f84953a84b6ccad7d36b4dff44a44297939a82bd07", "0x846a01e741874ae560eee20e67469f658e403ae9f52a986b83c701b6ab0f92dad8715004d3b13eeeec97cfdf935b2ac7", "0xb3e4dba507a119489fce9f6b078fcfa8cec5a3671b848433ad256777c1f4b83bfef3b5292aea46ce3d0567d64ee542f1", "0xa29cf99d73173583d3adeb3c5ef3d586f5ac32b9e313737821e39eae143dcd14381486dca1665f09231c7dad470875ac", "0xa925303aebf4a2cec7053b841f93d7bbd77d387625f6fb0480825956b64430d8ea31b2fc4cc4b0900a3525aaf75db87b", "0xb31ea58b3d784dbb83a76def08526a4cbdfd82a1d5447d79472b1d5465f9cea4429bfde79847d9036594e82c93221891", "0x9499ea5d1636d3b8314fcad83b5a5d4b6ab28b93b0b8e42a2440e53655d686d52a03e7fc1eec4b333b6774263a4050e4", "0x81e9b18de784c2ac478312a333b7af91b4527fdb991009a07e88bbb204c874492bb018a5b6489f39a13eacbcd4065584", "0xb99d916db5557db28e80ca0c8b79862ae016f6d5f5f2bb36062fc23827375f14794fce94c37218f66b5d81165975ee47", "0x988a02856544bd75fc1b249faca37f73c89078351a197e3179764c7da864e4850781106bcb7f7988b2c2c9755cc219cf", "0xb0dc0aefa8a9aa2a04fb01ccaec2a5a32a26c2d0c580b3a88b630f592aae4f4fd277478ee56e2f79ae5eed1d8b8d8e8f", "0x99b394f746b8d6e3525b9b43e31aaec804f3d0f65bf2a80a503bfa45e1978ae0a8c1084f4657faf8d597a439e071c467", "0xb5890497081dad5dffb1df4063d7d0428677d263b595c592d4115745712f6478325a6db0edf573e3a65b724fa35ba8ff", "0xa32fb81eded1e46fabdd69e833666b748ca63719d0ff4f10d4fa58610f287f64acc6e0292572c999540bd5ead29d9ca6", "0x925488c7e7dd7f884888711a63655fe650a95ef7319347216868cc2d79fe4cf2068ef8cf666fa23843ab7991ce445bb2", "0xa9950b49d533e7ccf96cef1372a6b3d8908ac596cc0393050ac1bd9682a232e039f5e5f46e5b6da4be66b361c297bc63", "0x950b1c021246eca51f9749af1695728be470890503ced0bb8bd7b21e7cbd26fe8318b2fcf8746982d382b9430bd67928", "0x963b5b52a6454d6499212a653bce27cc4bd8011f7e5bf4c90080f555bf8dfd35c07b7f01888799dec552b593dc267634", "0xa93bd8e5f943e6770ced8f247db5745a2ef0f57921a7a7abc77ed197d78ff6d2f916fbc6269d0bfa7af8f8b82e14fe5d", "0xb894fa39a5aa80c4d0019e35a11bdd3a741710c3f6153458845c3f80ebf4b01c4ceef985cacaeb670bf189f8f6628064", "0xb6daa406c3096dfc8e120b8f8dbde716b040f39f4536298dbf3f63c13082be06ee185acb1d257b096157eb09429a4306", "0x8de524358c574022eb9e1f573b0dabf62789de21a281c0919a3de57ca758e0e5021da0eafa588eef1dcb296327254df5", "0x870ebbbcbb8878e3db7a86b536cd7670029d83240c8fbb048faea471cb8d199523be918eb1f4f2c150a397eb8b6d22d8", "0xa0a3f0380c78d2cbfad41087b1a1acde29893614795b5f1f9c4b2a185378b41daeec2c1e608e7e1ed8604ab9b2aab9f7", "0x9231e01294b1c5e890c697a9f6fcff25363e08033ee02d65931437e824231b166f94a25ddc33d2984a68a3c84ebb77c3", "0x978b738e84ba206614429fc9fc4f7ba84404d93106da1cc5af42d132bc76d44ac0ce487d08d8a31c1df50e0a0c52bcb1", "0x8a54d90b9b7a13df1737654a51318231a26a975111122b18fe9f1921843505b701d397ed92201ad01ddc03518966c1d4", "0x871b526e7874f7832212c446c6402a0e584621b626a5686aac4e71ca28ff5a4843bf27b739b60cf9b7e0b779dbb63909", "0x901f6e36eb301376f5094e23fdc7dbcb92a6dac13e1a6ab374b12217e99cfe71e49dc77b15b01fa67f98f20c7877ca69", "0x80ddb1044a73403a782cba6e53785b95a927c5d5345fedfaf97c0e19bc4ff3db3f41c4e664bf168095390c9151644f30", "0xb9845cceda7505b05ad2edfd474805d66bd1ecc4494eb7cc1c7bf7f42093e3067b224e73f10ebb70ec3eceb1b5fe1575", "0xa1ec790c19f4c193762fbc4d822420c16718c6358ff5d42132426f117a5ebeda57becccb66147f9a4749510d65f44717", "0x91e88d193ae37ced440ebf91ab358eb26376fe55de497f24514db09a693cb033711c7e7e64f3694f1cff44c0f11f20ea", "0xb873b980f52c3c18adb3323a6f653486c0e2f102d573dab9e0a86d354e2afb21cb71c4706e72fdc8e7c097fc6f65fce1", "0x99047d305ae4387d271440b96e6eafefe347003b0af10cf1e3790d2cefcd729cf9408c5ed3c5bb3dfa9b1c89ff633198", "0x8e7c53cffb2b435fb6d7e9faed393e191b916626fe8cb46be9dfa850df45c5feb4bd6572299e9f6a33dd07bb725c9143", "0x9791027c7f9fc91397b425623ac51f7b5a6546e3dda63c21f80b1d7dea1451eabc15aee677a6a5fa29d1fd8b4c64b36a", "0xb9f92498b2090acd1e1c92e8cdb38412756b256864d9b767860343b519877287186f96a68ddf4e00e469bf1fa971ec83", "0xafcc022ffd09961a2970010fa2c79f4185009f60fc87d82a148b551cd6269864cd249f9795cde70bd7a73f8f671019ef", "0x870b3ecb228d9aaa6b01ac5c780539f00abaff107223b1ee13e7fc5f535e40afa29e52247405d8e3dc9099e9e420cf19", "0xa8f4dd8c7867f255f474c636d19a8eb89a0298ef5affbd5a01ec28f0dc7bff40dc771f0ea5e22fc2f8c936395ed4fcb3", "0x8c45d2d979af7c0a5ecf968b28693a3a09fd0d55d48f316180863d6b1afb23ab6f79c16b2154862389714848c4afdd04", "0xaeeb8269a354bd335fae2d6d2db6be00a35819f590cd84e5a5099750f9d0de909a5508d889f5988a1892245996938c46", "0x95b7e936638ad6ac67aacd9d3425391b7e27c524be955257a2ebb4c5d2f2bd4399b6b476ffef48f28932d0a1b3ef38ec", "0xa0e9a78097a83fe8f9243cbdfd5ebcfd8cc7557d4ac4c1ad0a1faae976cf0961631afa575ed6b5424dbb695b48697315", "0xa646f121cb47a6553d8daef6e31643a345cec6d8febf8c8dc696ad295ea695849654f87e5dc13baef0276b0b36704340", "0x8b5bf226aaf03562b9b6d81cbce3b35bdf7b23f540f7cc6e6f2359f704227bd7696ca7a80d3f96197c81176f561a5cfc", "0xa45cfbd05853fedae909fd854b0d76b30ad5d0ed91b8b7f98fa40fc7382b3314030dd97fa167b3f2c5229c231cf06682", "0xaaa1af544b8dbf9de1592b30ad6e2c332738897e6044febde15559dc0302312bf43db977433b8d8f57098e3610b27d68", "0xa12692d12945703ee9b8bfc9c521b103547a849f264d54ba9f1074b3937e8ad0f2c5e564b17f6b2e2d259e75e96ed350", "0x86e165180ce54f7dc7cdae67e0574f97adea97ceac70233b8fd73c1416fe2e5aac2589772b687ca3692e05d1877adaa6", "0x8f49820c387c196b48da071680cb85de81a5b0f4ba333208bca087b56bea6b88cc26b9331643b6430793079f40d16168", "0x8a5f6f1efe095cdfd9069ccab623f23d87680a093ff62d43dccbe8bf0195abf87ce4969b9b4f5a5a092f5a7925df8777", "0xb5102773879ae4e7d04d5cde9adb9ff35fc981398ee6c485f3a926abe35a4ae72450700a7ec7450e676870b09b579d64", "0x87368193fa0e7c5c4fd7761084e2946b7b1bfcdb9b8a816976b0cc9ea292a34adfe25f2f0cc4fbb2c4d228c1cc093cfb", "0xb7e5263d4cf905c62f3f9d124660cdf9a16980bdc8b1c716699746dbd88c85af74f5965784efe687664daf5deb5e88aa", "0xb119613faece054278ff0fba2c8dd2eb1cef428d3f263e68c3a9a437d7ed730550aa6bbcb8873337bdaf1e06c535a1cd", "0x8e58234eb7a41a4f4885ad07fea47dd78b43fba5b8c2b4f1850fa07d3434f1dca8e631f5ebb60c184e472d6488f02fc7", "0xa4704ae5d9b1f65e8563bace2c919434ea84e3d911af43171de38bd3a68a9ecc6ebc6e0afbaf5773d46192cd1a87d1df", "0xa1b5279b46951089208889c7233ae80ccbf01430f62421f03ea63d87d88fb14989ef3ae3fd82a916c3cf264bbd9c91b6", "0xaacd0e42dfc5c3c1d4231d731e2517f4cfa1380124a702a6ee9839dcebabab8208fe73df87dd41346ee8d1f24c7ee3bf", "0x8bd9d9aedc0883ba324400361ab29a7a7db3fb50e023e606bca525e521725aa272563d271d1b8df9bd5e43627cf92717", "0xab6b057aab423fb178d79499b648caa16380020307e639e7e3a494b4335ed9030def7d6cc0b73afb8e518bbef1346131")
    print(hash_tree_root(x))

    # print(hash_tree_root(Vector[BLSPubkey, 4](
    #                      "0xa1b5279b46951089208889c7233ae80ccbf01430f62421f03ea63d87d88fb14989ef3ae3fd82a916c3cf264bbd9c91b6",
    #                      "0xaacd0e42dfc5c3c1d4231d731e2517f4cfa1380124a702a6ee9839dcebabab8208fe73df87dd41346ee8d1f24c7ee3bf",
    #                      "0x8bd9d9aedc0883ba324400361ab29a7a7db3fb50e023e606bca525e521725aa272563d271d1b8df9bd5e43627cf92717",
    #                      "0xab6b057aab423fb178d79499b648caa16380020307e639e7e3a494b4335ed9030def7d6cc0b73afb8e518bbef1346131")))

    # print(hash_tree_root(BLSPubkey("0xb829e2a55b46c3cfae524d4b3bbe6f54610fa598581d0cbf026ca8e8ab1967b17d6fbdf154dc32ffe7ca18ec6094d4bc")))


    """
    The following code is used to test bls signature verification, ignore it
    """
    # assert py_ecc_bls.FastAggregateVerify([
    # 		bytes.fromhex("a73eb991aa22cdb794da6fcde55a427f0a4df5a4a70de23a988b5e5fc8c4d844f66d990273267a54dd21579b7ba6a086"),
    # 		bytes.fromhex("b29043a7273d0a2dbc2b747dcf6a5eccbd7ccb44b2d72e985537b117929bc3fd3a99001481327788ad040b4077c47c0d"),
    # 		bytes.fromhex("b928f3beb93519eecf0145da903b40a4c97dca00b21f12ac0df3be9116ef2ef27b2ae6bcd4c5bc2d54ef5a70627efcb7"),
    # 		bytes.fromhex("9446407bcd8e5efe9f2ac0efbfa9e07d136e68b03c5ebc5bde43db3b94773de8605c30419eb2596513707e4e7448bb50"),
    # 	],
    # 	bytes.fromhex("69241e7146cdcc5a5ddc9a60bab8f378c0271e548065a38bcc60624e1dbed97f"),
    # 	bytes.fromhex("b204e9656cbeb79a9a8e397920fd8e60c5f5d9443f58d42186f773c6ade2bd263e2fe6dbdc47f148f871ed9a00b8ac8b17a40d65c8d02120c00dca77495888366b4ccc10f1c6daa02db6a7516555ca0665bca92a647b5f3a514fa083fdc53b6e")
    # 	)

    # my_hex = "d5722733abc981a2e933beb7b1d306ba201e6b3309e44f859a30ab45d85f6669"
    # my_bytes = bytes.fromhex(my_hex)
    # print(my_bytes)

    # print(bytes.fromhex("a73eb991aa22cdb794da6fcde55a427f0a4df5a4a70de23a988b5e5fc8c4d844f66d990273267a54dd21579b7ba6a086"))
    # assert py_ecc_bls.FastAggregateVerify([
    # 		BLSPubkey("0xa73eb991aa22cdb794da6fcde55a427f0a4df5a4a70de23a988b5e5fc8c4d844f66d990273267a54dd21579b7ba6a086"),
    # 		BLSPubkey("0xb29043a7273d0a2dbc2b747dcf6a5eccbd7ccb44b2d72e985537b117929bc3fd3a99001481327788ad040b4077c47c0d"),
    # 		BLSPubkey("0xb928f3beb93519eecf0145da903b40a4c97dca00b21f12ac0df3be9116ef2ef27b2ae6bcd4c5bc2d54ef5a70627efcb7"),
    # 		BLSPubkey("0x9446407bcd8e5efe9f2ac0efbfa9e07d136e68b03c5ebc5bde43db3b94773de8605c30419eb2596513707e4e7448bb50"),
    # 	],
    # 	bytes.fromhex("0x69241e7146cdcc5a5ddc9a60bab8f378c0271e548065a38bcc60624e1dbed97f"),
    # 	BLSSignature("0xb204e9656cbeb79a9a8e397920fd8e60c5f5d9443f58d42186f773c6ade2bd263e2fe6dbdc47f148f871ed9a00b8ac8b17a40d65c8d02120c00dca77495888366b4ccc10f1c6daa02db6a7516555ca0665bca92a647b5f3a514fa083fdc53b6e")
    # 	)
