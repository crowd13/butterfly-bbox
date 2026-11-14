/* The platform exposes TolokaTask, not a global Task. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../interface');
function TolokaTask(options) { this.options = options; }
TolokaTask.prototype.getTask = function () { return this.options.task; };
TolokaTask.prototype.validate = function () { return null; };
const context = {exports: {}, TolokaTask};
vm.createContext(context);
const script = fs.readFileSync(path.join(root, 'editor.js'), 'utf8') + '\n' +
  fs.readFileSync(path.join(root, 'training.js'), 'utf8').replaceAll('__TRAINING_IOU__', '0.85') + '\n' +
  fs.readFileSync(path.join(root, 'task.js'), 'utf8').replace('__PRACTICE_JSON__', '[]').replace('__EXAM_PHOTO_IDS__', '[]');
assert.equal(context.Task, undefined);
vm.runInContext(script, context, {filename: 'task-source.js'});
const task = new context.exports.Task({task: {id: 'smoke'}});
assert(task instanceof TolokaTask);
assert.equal(task.validate().task_id, 'smoke');
let destroyed = false;
task.bboxEditor = {validate: () => '', destroy: () => { destroyed = true; }};
assert.equal(task.validate(), null);
task.onDestroy();
assert(destroyed);
console.log('Platform adapter initialization passed without a global Task');
