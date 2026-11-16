function ReviewTask(options) { TolokaTask.call(this, options); }
ReviewTask.prototype = Object.create(TolokaTask.prototype);
ReviewTask.prototype.constructor = ReviewTask;
ReviewTask.prototype.onRender = function () {
  var root = this.getDOMElement();
  var input = this.getTask().input_values;
  var image = root.querySelector("img");
  var boxes = root.querySelector(".review-boxes");
  var status = root.querySelector(".review-status");
  var solution = (this.getSolution() || {}).output_values || {};
  var self = this;
  image.src = input.image_url;
  image.onload = function () { status.textContent = ""; };
  image.onerror = function () { status.textContent = "Изображение не загрузилось"; };
  (input.source_boxes || []).forEach(function (box) {
    var node = document.createElement("div");
    node.className = "review-box";
    node.style.left = 100 * box.left + "%";
    node.style.top = 100 * box.top + "%";
    node.style.width = 100 * box.width + "%";
    node.style.height = 100 * box.height + "%";
    boxes.appendChild(node);
  });
  if (!input.source_boxes.length) status.textContent = "Исполнитель не поставил рамки";
  root.querySelector(".review-toggle").onclick = function (event) {
    var hidden = boxes.hidden = !boxes.hidden;
    event.currentTarget.textContent = hidden ? "Показать рамки" : "Скрыть рамки";
  };
  root.querySelectorAll("input[name=verdict]").forEach(function (radio) {
    radio.checked = radio.value === solution.verdict;
    radio.onchange = function () { self.setSolutionOutputValues({verdict: radio.value}); };
  });
};
ReviewTask.prototype.validate = function () {
  var value = (this.getSolution() || {}).output_values || {};
  if (!value.verdict) {
    return {task_id: this.getTask().id, errors: {verdict: {code: "REQUIRED", message: "Выберите ответ"}}};
  }
  return TolokaTask.prototype.validate.apply(this, arguments);
};
exports.Task = ReviewTask;
