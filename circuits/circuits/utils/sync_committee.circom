/*
Credits: https://github.com/succinctlabs/telepathy-circuits
*/

pragma circom 2.0.5;

include "./pairing/bls_signature.circom";
include "./pairing/curve.circom";
include "./pairing/bls12_381_func.circom";
include "./hash_to_field.circom";
include "./sha256.circom";

/*
 * Implements all logic regarding verifying the sync committee validator set
 * and signature verification. This template is quite expensive and takes
 * over 20M constraints (which dominates the cost of Step).
 */

template VerifySyncCommitteeSignature(
    SYNC_COMMITTEE_SIZE,
    LOG_2_SYNC_COMMITTEE_SIZE,
    N,
    K
) {
    signal input pubkeys[SYNC_COMMITTEE_SIZE][2][K];
    signal input aggregationBits[SYNC_COMMITTEE_SIZE];
    signal input signature[2][2][K];
    signal input signingRoot[32];
    signal input syncCommitteeRoot;
    signal output participation;

    /* RANGE CHECK AGGREGATION BITS */
    for (var i = 0; i < SYNC_COMMITTEE_SIZE; i++) {
        aggregationBits[i] * (aggregationBits[i] - 1) === 0;
    }

    /* HASH SIGNING ROOT TO FIELD */
    component hashToField = HashToField(32);
    for (var i = 0; i < 32; i++) {
        hashToField.msg[i] <== signingRoot[i];
    }

    /* VALIDATE PUBKEYS AGAINST SYNC COMMITTEE ROOT */
    component computeSyncCommitteeRoot = PoseidonG1Array(
        SYNC_COMMITTEE_SIZE,
        N,
        K
    );
    for (var i = 0; i < SYNC_COMMITTEE_SIZE; i++) {
        for (var j = 0; j < K; j++) {
            computeSyncCommitteeRoot.pubkeys[i][0][j] <== pubkeys[i][0][j];
            computeSyncCommitteeRoot.pubkeys[i][1][j] <== pubkeys[i][1][j];
        }
    }
    syncCommitteeRoot === computeSyncCommitteeRoot.out;

    /* COMPUTE AGGREGATE PUBKEY BASED ON AGGREGATION BITS */
    component getAggregatePublicKey = G1AddMany(
        SYNC_COMMITTEE_SIZE,
        LOG_2_SYNC_COMMITTEE_SIZE,
        N,
        K
    );
    for (var i = 0; i < SYNC_COMMITTEE_SIZE; i++) {
        getAggregatePublicKey.bits[i] <== aggregationBits[i];
        for (var j = 0; j < 2; j++) {
            for (var l = 0; l < K; l++) {
                getAggregatePublicKey.pubkeys[i][j][l] <== pubkeys[i][j][l];
            }
        }
    }
    getAggregatePublicKey.isPointAtInfinity === 0;

    /* VERIFY BLS SIGNATURE */
    component verifySignature = CoreVerifyPubkeyG1(N, K);
    for (var i = 0; i < 2; i++) {
        for (var j = 0; j < K; j++) {
            verifySignature.pubkey[i][j] <== getAggregatePublicKey.out[i][j];
            verifySignature.signature[0][i][j] <== signature[0][i][j];
            verifySignature.signature[1][i][j] <== signature[1][i][j];
            verifySignature.hash[0][i][j] <== hashToField.out[0][i][j];
            verifySignature.hash[1][i][j] <== hashToField.out[1][i][j];
        }
    }

    /* COMPUTE SYNC COMMITTEE PARTICIPATION */
    var computedParticipation = 0;
    for (var i = 0; i < SYNC_COMMITTEE_SIZE; i++) {
        computedParticipation += aggregationBits[i];
    }
    participation <== computedParticipation;
    component zeroCheck = IsZero();
    zeroCheck.in <== computedParticipation;
    zeroCheck.out === 0;
}