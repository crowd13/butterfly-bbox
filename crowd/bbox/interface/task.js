/* The builder inserts only public practice examples here, never exam/control solutions. */
var BBOX_PRACTICE = __PRACTICE_JSON__;
// Public image IDs identify the exam; no exam answers are embedded.
var BBOX_EXAM_IDS = __EXAM_PHOTO_IDS__;
function ButterflyTask(options) { TolokaTask.call(this, options); }
ButterflyTask.prototype = Object.create(TolokaTask.prototype);
ButterflyTask.prototype.constructor = ButterflyTask;
ButterflyTask.prototype.onRender = function () {
  var self = this;
  var root = this.getDOMElement();
  var example = BBOX_PRACTICE.find(function (row) {
    return String(row.photo_id) === String(self.getTask().input_values.photo_id);
  });
  this.bboxEditor = BBoxEditor(root.querySelector(".bbox-host"), this.getTask().input_values,
    (this.getSolution() || {}).output_values, function (value) {
      // Native training compares exact values. Normalize only a correct practice drawing.
      self.setSolutionOutputValues(example && bboxTrainingMatches(value, example.solution)
        ? structuredClone(example.solution) : value);
    });
  var practice = root.querySelector(".bbox-learning");
  this.practiceEditors = [];
  if (example) {
    var reveal = document.createElement("button"); reveal.type = "button";
    reveal.textContent = "Показать эталон и объяснение";
    var hint = document.createElement("p");
    reveal.addEventListener("click", function () {
      self.bboxEditor.reveal(example.solution); hint.textContent = example.hint;
    });
    practice.appendChild(reveal); practice.appendChild(hint);
    return;
  }
  if (!BBOX_EXAM_IDS.includes(String(this.getTask().input_values.photo_id))) {
    practice.remove();
    return;
  }
  var examples = document.createElement("details");
  var summary = document.createElement("summary");
  summary.textContent = "Учебные примеры";
  examples.appendChild(summary);
  var note = document.createElement("p");
  note.textContent = "Учебные ответы не отправляются и не влияют на экзамен. Зелёные рамки — эталон.";
  examples.appendChild(note);
  practice.appendChild(examples);
  practice = examples;
  BBOX_PRACTICE.forEach(function (example, index) {
    var item = document.createElement("details");
    var title = document.createElement("summary");
    title.textContent = "Учебный пример " + (index+1);
    item.appendChild(title);
    var host = document.createElement("div"); item.appendChild(host);
    var editor;
    item.addEventListener("toggle", function () {
      if (item.open && !editor) {
        editor = BBoxEditor(host, example, null, function () {});
        self.practiceEditors.push(editor);
        var reveal = document.createElement("button"); reveal.type = "button";
        reveal.textContent = "Показать эталон и объяснение";
        var hint = document.createElement("p");
        reveal.addEventListener("click",function () {editor.reveal(example.solution); hint.textContent = example.hint;});
        item.appendChild(reveal); item.appendChild(hint);
      }
    });
    practice.appendChild(item);
  });
};
ButterflyTask.prototype.validate = function () {
  var message = this.bboxEditor ? this.bboxEditor.validate() : "Дождитесь загрузки редактора";
  if (message) return {task_id:this.getTask().id, errors:{status:{code:"INVALID",message:message}}};
  return TolokaTask.prototype.validate.apply(this, arguments);
};
ButterflyTask.prototype.onDestroy = function () {
  if (this.bboxEditor) this.bboxEditor.destroy();
  (this.practiceEditors || []).forEach(function (editor) {editor.destroy();});
};
exports.Task = ButterflyTask;
