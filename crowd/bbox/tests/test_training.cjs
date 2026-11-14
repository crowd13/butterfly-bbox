/* Run with node crowd/bbox/tests/test_training.cjs. No platform access. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../interface/training.js'), 'utf8')
  .replaceAll('__TRAINING_IOU__', '0.85'), context);
const matches = context.bboxTrainingMatches;
const box = {left:0.1, top:0.1, width:0.3, height:0.3};
const answer = boxes => ({status:'boxes', boxes});
const gold = answer([box]);
assert(matches(answer([{...box, left:0.11}]), gold));
assert(!matches(answer([{...box, left:0.2}]), gold));
assert(!matches(answer([box, box]), gold));
assert(!matches(answer([]), gold));
assert(!matches({status:'load_error', boxes:[]}, {status:'no_butterflies', boxes:[]}));
assert(matches({status:'no_butterflies', boxes:[]}, {status:'no_butterflies', boxes:[]}));
const second = {...box, left:0.6};
assert(matches(answer([second, box]), answer([box, second])));
// Matching must reassign an earlier choice, not greedily reject a valid pairing.
const left = {...box, width:0.5};
const right = {...left, left:0.14};
assert(matches(answer([{...left,left:0.12}, {...left,left:0.08}]), answer([left,right])));
console.log('Training IoU checks passed');
