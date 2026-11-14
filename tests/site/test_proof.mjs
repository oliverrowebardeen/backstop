import {test} from 'node:test';
import assert from 'node:assert/strict';
import {proofMetrics,efficientFrontier} from '../../ui/proof.js';
const block=(correct,wrong,cost)=>({correct,wrong,declined:10-correct-wrong,accuracy:correct/10,cost_per_q_total:cost});
test('claims reverse when Backstop does worse; they are not hardcoded wins',()=>{
 const data={n_test:10,setups:{single:block(7,2,.01),telem_default:block(9,1,.02),second_opinion:block(6,3,.03)}};
 const p=proofMetrics(data);assert.equal(p.wrong,-50);assert.ok(p.accuracy<0);assert.ok(p.cost<0);
});
test('missing and incomplete results remain placeholders',()=>{
 assert.equal(proofMetrics(null),null);
 assert.equal(proofMetrics({n_test:10,setups:{single:block(7,2,.01)}}),null);
});
test('Backstop is only on the frontier when it is actually efficient',()=>{
 const points=[{key:'telem_default',cost:1,accuracy:.9},{key:'second_opinion',cost:2,accuracy:.8},{key:'single',cost:.5,accuracy:.6}];
 assert.deepEqual(efficientFrontier(points).map(p=>p.key),['single','telem_default']);
});
