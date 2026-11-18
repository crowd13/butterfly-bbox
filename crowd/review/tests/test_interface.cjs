const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  const classes = new Set();
  return {
    children: [], style: {}, hidden: false, attributes: {},
    appendChild(child) { this.children.push(child); },
    setAttribute(name, value) { this.attributes[name] = value; },
    scrollIntoView() { this.scrolled = true; },
    classList: {
      remove(name) { classes.delete(name); },
      toggle(name) {
        if (classes.has(name)) { classes.delete(name); return false; }
        classes.add(name); return true;
      },
    },
    set textContent(value) { this.text = value; this.children = []; },
    get textContent() { return this.text; },
  };
}

function TolokaTask(options) { this.options = options; this.solution = {output_values: {}}; }
function TolokaAssignment() {}
TolokaTask.prototype.getTask = function () { return this.options.task; };
TolokaTask.prototype.getDOMElement = function () { return this.options.root; };
TolokaTask.prototype.getSolution = function () { return this.solution; };
TolokaTask.prototype.setSolutionOutputValues = function (value) { this.solution.output_values = value; };
TolokaTask.prototype.validate = function () { return null; };
const context = {exports: {}, TolokaTask, TolokaAssignment, document: {createElement: element}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../interface/task.js'), 'utf8'), context);

function mount(sourceBoxes = [], withPractice = false) {
  const nodes = Object.fromEntries([
    'img', '.review-boxes', '.review-status', '.review-summary', '.review-retry',
    '.review-viewport', '.review-toggle', '.review-zoom', '.review-error',
  ].map(selector => [selector, element()]));
  const radios = ['OK', 'MISSING', 'EXTRA', 'BOUNDARY', 'LOAD_ERROR'].map(value => ({...element(), value}));
  const buttons = [nodes['.review-retry'], nodes['.review-toggle'], nodes['.review-zoom']];
  nodes['.review-photo'] = nodes.img;
  nodes['.review-main-boxes'] = nodes['.review-boxes'];
  nodes['.review-main-viewport'] = nodes['.review-viewport'];
  const practice = Object.fromEntries([
    'photo', 'boxes', 'progress', 'exercise', 'complete', 'status', 'feedback',
    'check', 'next', 'retry',
  ].map(name => ['.review-practice-' + name, element()]));
  const practiceRadios = radios.map(radio => ({...element(), value: radio.value}));
  const panel = element();
  panel.hidden = true;
  panel.querySelector = selector => practice[selector];
  panel.querySelectorAll = () => practiceRadios;
  nodes['.review-practice'] = withPractice ? panel : null;
  const root = {
    querySelector(selector) { return nodes[selector]; },
    querySelectorAll(selector) { return selector.startsWith('button') ? buttons.concat(radios) : radios; },
  };
  Object.defineProperty(nodes.img, 'src', {set() { if (nodes.img.onload) nodes.img.onload(); }});
  Object.defineProperty(practice['.review-practice-photo'], 'src', {
    set(value) {
      this.currentSource = value;
      if (this.onload) this.onload();
    },
  });
  const task = new context.exports.Task({root, task: {id: 'test', input_values: {
    image_url: 'https://example.org/image.jpg', source_boxes: sourceBoxes,
  }}});
  task.onRender();
  return {task, nodes, radios, panel, practice, practiceRadios};
}

const first = mount([{left: 0.1, top: 0.2, width: 0.3, height: 0.4}]);
const second = mount();
assert.notEqual(first.radios[0].name, second.radios[0].name, 'each task needs a separate browser radio group');
assert(first.radios.every(radio => radio.name === first.radios[0].name));
assert.equal(first.task.imageState, 'loaded', 'cached image loads must not be missed');
assert.equal(first.nodes['.review-boxes'].children[0].style.left, '10%');
assert.equal(first.nodes['.review-boxes'].children[0].style.height, '40%');
assert.match(second.nodes['.review-summary'].textContent, /Рамок нет/);
assert.match(first.task.validate().errors.verdict.message, /Выберите ответ/);
assert.equal(first.nodes['.review-error'].textContent, 'Выберите ответ');
assert.equal(first.nodes['.review-error'].hidden, false);
assert.equal(first.nodes['.review-error'].scrolled, true);
assert.equal(first.radios[0].attributes['aria-invalid'], 'true');
first.radios[0].onchange();
assert.equal(first.task.getSolution().output_values.verdict, 'OK');
assert.equal(first.nodes['.review-error'].hidden, true);
assert.equal(first.radios[0].attributes['aria-invalid'], 'false');
assert.equal(first.task.validate(), null);
first.task.onRender();
assert.equal(first.nodes['.review-boxes'].children.length, 1, 'rerender must not duplicate overlays');
assert(first.radios[0].checked, 'saved answer must be restored');

first.nodes.img.onerror();
assert.equal(first.task.imageState, 'error');
assert.equal(first.nodes['.review-retry'].hidden, false);
assert.equal(first.nodes['.review-boxes'].hidden, true);
assert.equal(first.nodes['.review-toggle'].disabled, true);
assert.match(first.task.validate().errors.verdict.message, /загрузки/);
first.radios[4].onchange();
assert.equal(first.task.validate(), null);
first.nodes['.review-retry'].onclick();
assert.equal(first.task.imageState, 'loaded');
assert.match(first.task.validate().errors.verdict.message, /Фотография загрузилась/);
assert.equal(first.nodes['.review-retry'].hidden, true);
assert.equal(first.nodes['.review-boxes'].hidden, false);
assert.equal(first.nodes['.review-toggle'].disabled, false);

const trainingHint = 'В кадре куколка, а не взрослая бабочка. <b>Текст, не HTML</b>';
first.task.onValidationFail({task_id: 'test', errors: {
  verdict: {code: 'INCORRECT_SOLUTION', message: trainingHint},
}});
assert.equal(first.nodes['.review-error'].textContent, trainingHint);
assert.equal(first.nodes['.review-error'].hidden, false);
assert.equal(first.radios[0].attributes['aria-describedby'], first.nodes['.review-error'].id);
assert.notEqual(first.nodes['.review-error'].id, second.nodes['.review-error'].id);
assert.equal(second.nodes['.review-error'].hidden, true, 'another card must not receive the hint');
assert.equal(second.radios[0].attributes['aria-invalid'], 'false');
first.radios[2].onchange();
assert.equal(first.task.getSolution().output_values.verdict, 'EXTRA');
assert.equal(first.nodes['.review-error'].hidden, true, 'changing the answer clears the old hint');
assert.equal(first.task.validate(), null, 'correctness remains the server responsibility');
first.task.onValidationFail({errors: {verdict: {code: 'INCORRECT_SOLUTION'}}});
assert.match(first.nodes['.review-error'].textContent, /Ответ неверный/);
assert.equal(first.task.validate(), null);
assert.equal(first.nodes['.review-error'].textContent, '', 'successful validation clears the hint');
delete first.nodes['.review-error'].scrollIntoView;
first.task.onValidationFail({errors: {verdict: {code: 'INVALID'}}});
assert.match(first.nodes['.review-error'].textContent, /Ответ не принят/);

first.nodes['.review-toggle'].onclick({currentTarget: first.nodes['.review-toggle']});
assert.equal(first.nodes['.review-boxes'].hidden, true);
first.nodes['.review-zoom'].onclick();
assert.equal(first.nodes['.review-zoom'].textContent, 'Показать весь кадр');
first.task.onDestroy();
assert.equal(first.nodes.img.onerror, null);
assert.equal(first.radios[0].onchange, null);

context.reviewPractice = ['OK', 'MISSING', 'EXTRA', 'BOUNDARY', 'OK', 'OK'].map((verdict, index) => ({
  image_url: 'https://example.org/practice-' + index + '.jpg',
  source_boxes: [{left: 0.1, top: 0.2, width: 0.3, height: 0.4}],
  verdict, hint: 'Объяснение примера ' + (index + 1),
}));
const lesson = mount([], true);
const neighbor = mount([], true);
assert.equal(lesson.panel.hidden, false);
assert.equal(neighbor.panel.hidden, true, 'only the first task may show the practice panel');
assert.equal(lesson.practiceRadios[0].name, lesson.radios[0].name + '-practice');
assert.notEqual(lesson.practiceRadios[0].name, lesson.radios[0].name);
lesson.radios[1].onchange();
const mainAnswer = lesson.task.getSolution().output_values.verdict;
const practiceNode = name => lesson.practice['.review-practice-' + name];
practiceNode('photo').onerror();
assert.equal(practiceNode('boxes').hidden, true);
assert.equal(practiceNode('retry').hidden, false);
practiceNode('check').onclick();
assert.equal(practiceNode('progress').textContent, 'Учебный пример 1 из 6');
practiceNode('retry').onclick();
assert.equal(practiceNode('check').disabled, false);
assert.equal(practiceNode('retry').hidden, true);
assert.equal(practiceNode('boxes').hidden, false);
practiceNode('check').onclick();
assert.equal(practiceNode('feedback').textContent, 'Выберите учебный ответ');
practiceNode('next').onclick();
assert.equal(practiceNode('progress').textContent, 'Учебный пример 1 из 6');
lesson.practiceRadios[1].onchange();
practiceNode('check').onclick();
assert.match(practiceNode('feedback').textContent, /Пока неверно.*Объяснение примера 1/);
assert.equal(practiceNode('next').hidden, true);
assert.equal(practiceNode('progress').textContent, 'Учебный пример 1 из 6');
assert.equal(lesson.task.validate(), null, 'optional practice must not gate the actual task');
for (let index = 0; index < 6; index++) {
  assert.equal(practiceNode('progress').textContent, 'Учебный пример ' + (index + 1) + ' из 6');
  assert.equal(practiceNode('photo').currentSource, context.reviewPractice[index].image_url);
  assert.equal(practiceNode('boxes').children.length, 1);
  assert.equal(practiceNode('boxes').children[0].style.left, '10%');
  const radio = lesson.practiceRadios.find(radio => radio.value === context.reviewPractice[index].verdict);
  radio.onchange();
  practiceNode('check').onclick();
  assert.match(practiceNode('feedback').textContent, /^Верно\./);
  assert.equal(practiceNode('next').hidden, false);
  assert.equal(lesson.task.getSolution().output_values.verdict, mainAnswer, 'practice must not write the working answer');
  practiceNode('next').onclick();
}
assert.equal(practiceNode('progress').textContent, 'Примеры завершены');
assert.equal(practiceNode('exercise').hidden, true);
assert.equal(practiceNode('complete').hidden, false);
practiceNode('next').onclick();
practiceNode('check').onclick();
assert.equal(practiceNode('progress').textContent, 'Примеры завершены', 'the lesson must not cycle after its sixth example');
lesson.task.onRender();
assert.equal(practiceNode('progress').textContent, 'Примеры завершены', 'rerendering the same task must not restart the lesson');
assert.equal(lesson.task.getSolution().output_values.verdict, mainAnswer);
lesson.task.onDestroy();
assert.equal(practiceNode('photo').onload, null);
assert.equal(lesson.practiceRadios[0].onchange, null);
const nextPage = mount([], true);
assert.equal(nextPage.panel.hidden, false, 'the next page may claim its own optional practice panel');
nextPage.task.onDestroy();
console.log('Review interface checks passed');
