/* The builder inserts only public practice examples here, never exam/control solutions. */
var BBOX_PRACTICE = __PRACTICE_JSON__;
function ButterflyTask(options) { Task.call(this, options); }
ButterflyTask.prototype = Object.create(Task.prototype);
ButterflyTask.prototype.constructor = ButterflyTask;
ButterflyTask.prototype.onRender = function () {
  var self = this;
  var root = this.getDOMElement();
  this.bboxEditor = BBoxEditor(root.querySelector(".bbox-host"), this.getTask().input_values,
    (this.getSolution() || {}).output_values, function (value) { self.setSolutionOutputValues(value); });
  var practice = root.querySelector(".bbox-practice");
  this.practiceEditors = [];
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
  return Task.prototype.validate.apply(this, arguments);
};
ButterflyTask.prototype.onDestroy = function () {
  if (this.bboxEditor) this.bboxEditor.destroy();
  (this.practiceEditors || []).forEach(function (editor) {editor.destroy();});
};
exports.Task = ButterflyTask;
