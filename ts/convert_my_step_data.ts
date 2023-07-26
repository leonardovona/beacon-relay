/* 
* Adapted from: https://github.com/succinctlabs/eth-proof-of-consensus/blob/main/circuits/test/generate_input_data.ts
*/
import path from "path";
import fs from "fs";

import { PointG1 } from "@noble/bls12-381";

import {
  hexToIntArray,
  bigint_to_array,
  sigHexAsSnarkInput,
  msg_hash
} from "./bls_utils";

(BigInt.prototype as any).toJSON = function () {
  return this.toString();
};

var n: number = 55;
var k: number = 7;

function point_to_bigint(point: PointG1): [bigint, bigint] {
  let [x, y] = point.toAffine();
  return [x.value, y.value];
}

/*
* Convert data to a suitable format for signature verification circuit.
* Pubkeys are converted first to G1 points and then to bigints.
* A similar process is followed for the signature and the signing root.
* The input data is taken from a file in the data folder.
* The output is written to a file in the data folder.
*/
async function convertMyStepData(b: number = 512) {
  const dirname = path.resolve();
  const rawData = fs.readFileSync(
    path.join(dirname, "data/my_step_data.json")
  );
  const myStepData = JSON.parse(rawData.toString());

  const pubkeys = myStepData.pubkeys.map((pubkey: any, idx: number) => {
    const point = PointG1.fromHex((pubkey).substring(2));
    const bigints = point_to_bigint(point);
    return [
      bigint_to_array(n, k, bigints[0]),
      bigint_to_array(n, k, bigints[1]),
    ];
  });

  const pubkeysX = new Array<Array<number>>();
  const pubkeysY = new Array<Array<number>>();

  for(let i = 0; i < pubkeys.length; i++) {
    pubkeysX.push(pubkeys[i][0]);
    pubkeysY.push(pubkeys[i][1]);
  }

  const myStepInput = {
    pubkeysX: pubkeysX,
    pubkeysY: pubkeysY,
    aggregationBits: myStepData.pubkeybits,
    signature: sigHexAsSnarkInput(myStepData.signature, "array"),
    signingRoot: hexToIntArray(myStepData.signing_root),
    participation: myStepData.participation,
    syncCommitteePoseidon: myStepData.syncCommitteePoseidon,
  };

  const myStepInputFilename = path.join(
    dirname,
    "data",
    `my_step_input.json`
  );
  
  console.log("Writing input to file", myStepInputFilename);
  fs.writeFileSync(
    myStepInputFilename,
    JSON.stringify(myStepInput)
  );
}

convertMyStepData();