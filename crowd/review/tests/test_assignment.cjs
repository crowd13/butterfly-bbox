const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function TolokaTask() {}
function TolokaAssignment(options) {
  this.options = options;
  this.baseInitialized = true;
}
TolokaAssignment.prototype.getId = function () { return this.options.id; };
TolokaAssignment.prototype.getTaskSuite = function () { return this.options.suite; };
const capturedOnValidationDone = function (event) {
  this.getTaskSuite().onValidationDone(event.data.correctTrainingSolutions);
};
TolokaAssignment.prototype.onValidationDone = capturedOnValidationDone;
let failedEvent;
const failResult = {};
TolokaAssignment.prototype.onValidationFail = function (event) {
  failedEvent = event;
  return failResult;
};
const context = {exports: {}, TolokaTask, TolokaAssignment};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../interface/task.js'), 'utf8'), context);
const Assignment = context.exports.Assignment;
const capturedTaskSuiteOnValidationDone = function (response) {
  var _this6 = this;
  if (response) {
    response.forEach(function (item) {
      item && _this6.getTasksIndexed()[item.task_id].onValidationDone(item);
    });
  }
};
const capturedTaskOnValidationDone = function () {};
const completions = [];
const taskCallbacks = [];
const task0 = {
  onValidationDone(item) {
    taskCallbacks.push(item);
    return capturedTaskOnValidationDone.apply(this, arguments);
  },
};
const suite = {
  getTasksIndexed() { return {'0': task0}; },
  onValidationDone(value) {
    completions.push(value);
    return capturedTaskSuiteOnValidationDone.apply(this, arguments);
  },
};
const preview = new Assignment({suite});
assert(preview instanceof TolokaAssignment);
assert.equal(preview.baseInitialized, true);
assert.throws(() => capturedOnValidationDone.call(preview, {}), /correctTrainingSolutions/);

const emptyEvents = [{}, {data: undefined}, Object.freeze({}), Object.freeze({data: undefined})];
for (const event of emptyEvents) {
  const keys = Object.keys(event);
  assert.equal(preview.onValidationDone(event), undefined);
  assert.deepEqual(Object.keys(event), keys, 'the guard must not mutate its event');
  assert.equal(event.data, undefined);
}
assert.equal(completions.length, emptyEvents.length, 'normalized preview events must still invoke the base lifecycle');
assert(completions.every(value => value === undefined), 'missing metadata must not invent server corrections');
assert.equal(taskCallbacks.length, 0, 'missing preview metadata must not mark a task correct');

const correction = Object.freeze({task_id: '0', output_values: Object.freeze({verdict: 'OK'})});
const corrections = Object.freeze([correction]);
const valid = Object.freeze({data: Object.freeze({correctTrainingSolutions: corrections})});
assert.equal(preview.onValidationDone(valid), undefined, 'preserve the captured base return value');
assert.equal(completions[completions.length - 1], corrections, 'forward the server correction array unchanged');
assert.equal(taskCallbacks.length, 1);
assert.equal(taskCallbacks[0], correction, 'dispatch the exact server correction to its task');
const flat = Object.freeze({correctTrainingSolutions: corrections});
preview.onValidationDone(flat);
assert.equal(flat.data, undefined);
assert.equal(completions[completions.length - 1], corrections, 'preserve existing corrections even without an envelope');
assert.equal(taskCallbacks[taskCallbacks.length - 1], correction);
assert.throws(() => preview.onValidationDone({data: null}), /correctTrainingSolutions/);
assert.throws(() => preview.onValidationDone(undefined), /data/);
assert.throws(() => preview.onValidationDone(null), /data/);

const worker = new Assignment({id: 'real-assignment', suite});
assert.throws(() => worker.onValidationDone({}), /correctTrainingSolutions/);
assert.throws(() => worker.onValidationDone({data: undefined}), /correctTrainingSolutions/);
assert.throws(() => worker.onValidationDone({data: null}), /correctTrainingSolutions/);
const nullId = new Assignment({id: null, suite});
assert.throws(() => nullId.onValidationDone({}), /correctTrainingSolutions/, 'only strict undefined means preview');

const delegated = [];
const returnValue = {};
TolokaAssignment.prototype.onValidationDone = function () {
  delegated.push({receiver: this, args: [...arguments]});
  capturedOnValidationDone.apply(this, arguments);
  return returnValue;
};
const extraArgument = {};
const emptyFrozen = Object.freeze({name: 'assignment:validation:done', data: undefined});
const callbackCount = taskCallbacks.length;
assert.equal(preview.onValidationDone(emptyFrozen, extraArgument), returnValue);
assert.equal(delegated[0].receiver, preview);
assert.notEqual(delegated[0].args[0], emptyFrozen);
assert.equal(delegated[0].args[0].name, emptyFrozen.name);
assert.deepEqual(Object.keys(delegated[0].args[0].data), []);
assert.equal(delegated[0].args[1], extraArgument);
assert.equal(emptyFrozen.data, undefined);
assert.equal(completions[completions.length - 1], undefined);
assert.equal(taskCallbacks.length, callbackCount);
assert.equal(preview.onValidationDone(valid, extraArgument), returnValue);
assert.equal(delegated[1].receiver, preview);
assert.equal(delegated[1].args[0], valid);
assert.equal(delegated[1].args[1], extraArgument);
assert.equal(worker.onValidationDone(valid, extraArgument), returnValue);
assert.equal(delegated[2].receiver, worker);
assert.equal(delegated[2].args[0], valid);
const ordinary = Object.freeze({data: Object.freeze({})});
assert.equal(worker.onValidationDone(ordinary), returnValue);
assert.equal(delegated[3].args[0], ordinary);
assert.equal(completions[completions.length - 1], undefined);

const wrong = Object.freeze({data: {incorrectTrainingSolutions: [{
  task_id: 'work-task', errors: {verdict: {code: 'INVALID', message: 'Server validation error'}},
}]}});
assert.equal(Assignment.prototype.onValidationFail, TolokaAssignment.prototype.onValidationFail);
assert.equal(worker.onValidationFail(wrong), failResult);
assert.equal(failedEvent, wrong, 'the preview guard must not change ordinary validation errors');
console.log('Preview assignment compatibility checks passed');
