var reviewTaskSequence = 0;
var reviewPracticeOwner = null;
function ReviewAssignment(options) { TolokaAssignment.call(this, options); }
ReviewAssignment.prototype = Object.create(TolokaAssignment.prototype);
ReviewAssignment.prototype.constructor = ReviewAssignment;
ReviewAssignment.prototype.onValidationDone = function (event) {
  if (this.getId() === undefined && event && event.data === undefined) {
    var args = Array.prototype.slice.call(arguments);
    var data = {};
    if (Object.prototype.hasOwnProperty.call(event, "correctTrainingSolutions")) {
      data.correctTrainingSolutions = event.correctTrainingSolutions;
    }
    args[0] = Object.assign({}, event, {data: data});
    return TolokaAssignment.prototype.onValidationDone.apply(this, args);
  }
  return TolokaAssignment.prototype.onValidationDone.apply(this, arguments);
};
function ReviewTask(options) {
  TolokaTask.call(this, options);
  this.radioGroup = "review-verdict-" + (++reviewTaskSequence);
}
ReviewTask.prototype = Object.create(TolokaTask.prototype);
ReviewTask.prototype.constructor = ReviewTask;
ReviewTask.prototype.onRender = function () {
  var root = this.getDOMElement();
  var input = this.getTask().input_values;
  var image = root.querySelector(".review-photo");
  var boxes = root.querySelector(".review-main-boxes");
  var status = root.querySelector(".review-status");
  var error = root.querySelector(".review-error");
  var summary = root.querySelector(".review-summary");
  var retry = root.querySelector(".review-retry");
  var viewport = root.querySelector(".review-main-viewport");
  var toggle = root.querySelector(".review-toggle");
  var zoom = root.querySelector(".review-zoom");
  var sourceBoxes = input.source_boxes || [];
  var solution = (this.getSolution() || {}).output_values || {};
  var self = this;
  error.id = this.radioGroup + "-error";
  function loadImage() {
    self.imageState = "loading";
    status.textContent = "Загрузка фотографии…";
    retry.hidden = true;
    boxes.hidden = true;
    toggle.disabled = zoom.disabled = true;
    toggle.textContent = "Скрыть рамки";
    toggle.setAttribute("aria-pressed", "false");
    image.src = input.image_url;
  }
  image.onload = function () {
    self.imageState = "loaded";
    status.textContent = "";
    retry.hidden = true;
    boxes.hidden = false;
    toggle.disabled = zoom.disabled = false;
  };
  image.onerror = function () {
    self.imageState = "error";
    status.textContent = "Фотография не загрузилась. Попробуйте загрузить её повторно или выберите «Ошибка загрузки».";
    retry.hidden = false;
    boxes.hidden = true;
    toggle.disabled = zoom.disabled = true;
  };
  retry.onclick = loadImage;
  summary.textContent = sourceBoxes.length
    ? "Предложено рамок: " + sourceBoxes.length
    : "Рамок нет. Проверьте, действительно ли на фотографии нет взрослых бабочек.";
  boxes.textContent = "";
  boxes.hidden = false;
  sourceBoxes.forEach(function (box) {
    var node = document.createElement("div");
    node.className = "review-box";
    node.style.left = 100 * box.left + "%";
    node.style.top = 100 * box.top + "%";
    node.style.width = 100 * box.width + "%";
    node.style.height = 100 * box.height + "%";
    boxes.appendChild(node);
  });
  toggle.textContent = "Скрыть рамки";
  toggle.setAttribute("aria-pressed", "false");
  toggle.onclick = function (event) {
    var hidden = boxes.hidden = !boxes.hidden;
    event.currentTarget.textContent = hidden ? "Показать рамки" : "Скрыть рамки";
    event.currentTarget.setAttribute("aria-pressed", String(hidden));
  };
  viewport.classList.remove("review-zoomed");
  zoom.textContent = "Увеличить до исходного размера";
  zoom.setAttribute("aria-pressed", "false");
  zoom.onclick = function () {
    var enlarged = viewport.classList.toggle("review-zoomed");
    zoom.textContent = enlarged ? "Показать весь кадр" : "Увеличить до исходного размера";
    zoom.setAttribute("aria-pressed", String(enlarged));
  };
  root.querySelectorAll(".review-verdicts input").forEach(function (radio) {
    radio.name = self.radioGroup;
    radio.setAttribute("aria-describedby", error.id);
    radio.checked = radio.value === solution.verdict;
    radio.onchange = function () {
      self.setSolutionOutputValues({verdict: radio.value});
      self.setValidationMessage("");
    };
  });
  this.setValidationMessage("");
  loadImage();
  this.renderPractice();
};
ReviewTask.prototype.renderPractice = function () {
  var panel = this.getDOMElement().querySelector(".review-practice");
  var examples = typeof reviewPractice === "undefined" ? [] : reviewPractice;
  if (!panel || !Array.isArray(examples) || examples.length !== 6 ||
      (reviewPracticeOwner && reviewPracticeOwner !== this)) return;
  reviewPracticeOwner = this;
  panel.hidden = false;
  if (this.practicePanel === panel && this.practiceDestroy) return;
  if (this.practiceDestroy) this.practiceDestroy();
  this.practicePanel = panel;
  var image = panel.querySelector(".review-practice-photo");
  var boxes = panel.querySelector(".review-practice-boxes");
  var progress = panel.querySelector(".review-practice-progress");
  var exercise = panel.querySelector(".review-practice-exercise");
  var complete = panel.querySelector(".review-practice-complete");
  var status = panel.querySelector(".review-practice-status");
  var feedback = panel.querySelector(".review-practice-feedback");
  var check = panel.querySelector(".review-practice-check");
  var next = panel.querySelector(".review-practice-next");
  var retry = panel.querySelector(".review-practice-retry");
  var radios = panel.querySelectorAll(".review-practice-verdicts input");
  exercise.hidden = false; complete.hidden = true;
  var index = 0, answer = "", correct = false, loaded = false, finished = false;
  var group = this.radioGroup + "-practice";
  radios.forEach(function (radio) {
    radio.name = group;
    radio.onchange = function () { answer = radio.value; feedback.textContent = ""; };
  });
  function show() {
    answer = ""; correct = loaded = false;
    progress.textContent = "Учебный пример " + (index + 1) + " из 6";
    feedback.textContent = "";
    status.textContent = "Загрузка учебной фотографии…";
    check.hidden = false; check.disabled = true;
    next.hidden = retry.hidden = true;
    next.textContent = index === 5 ? "Завершить примеры" : "Следующий пример";
    radios.forEach(function (radio) { radio.checked = false; radio.disabled = false; });
    boxes.textContent = ""; boxes.hidden = true;
    (examples[index].source_boxes || []).forEach(function (box) {
      var node = document.createElement("div");
      node.className = "review-box";
      node.style.left = 100 * box.left + "%"; node.style.top = 100 * box.top + "%";
      node.style.width = 100 * box.width + "%"; node.style.height = 100 * box.height + "%";
      boxes.appendChild(node);
    });
    image.src = examples[index].image_url;
  }
  image.onload = function () {
    loaded = true; status.textContent = ""; check.disabled = false; boxes.hidden = false;
  };
  image.onerror = function () {
    loaded = false; boxes.hidden = true; check.disabled = true; retry.hidden = false;
    status.textContent = "Учебная фотография не загрузилась. Можно повторить загрузку или свернуть тренировку и перейти к заданию.";
  };
  retry.onclick = show;
  check.onclick = function () {
    if (finished || !loaded || correct) return;
    if (!answer) { feedback.textContent = "Выберите учебный ответ"; return; }
    if (answer !== examples[index].verdict) {
      feedback.textContent = "Пока неверно. " + examples[index].hint;
      return;
    }
    correct = true;
    feedback.textContent = "Верно. " + examples[index].hint;
    check.hidden = true; next.hidden = false;
    radios.forEach(function (radio) { radio.disabled = true; });
  };
  next.onclick = function () {
    if (finished || !correct) return;
    if (index < 5) { index++; show(); return; }
    finished = true;
    exercise.hidden = true; complete.hidden = false;
    progress.textContent = "Примеры завершены";
  };
  this.practiceDestroy = function () {
    image.onload = image.onerror = null;
    check.onclick = next.onclick = retry.onclick = null;
    radios.forEach(function (radio) { radio.onchange = null; });
  };
  show();
};
ReviewTask.prototype.setValidationMessage = function (message) {
  var root = this.getDOMElement();
  var error = root.querySelector(".review-error");
  error.textContent = message;
  error.hidden = !message;
  root.querySelectorAll(".review-verdicts input").forEach(function (radio) {
    radio.setAttribute("aria-invalid", message ? "true" : "false");
  });
  if (message && typeof error.scrollIntoView === "function") {
    error.scrollIntoView({block: "nearest"});
  }
};
ReviewTask.prototype.onValidationFail = function (error) {
  var fields = (error || {}).errors || {};
  var messages = Object.keys(fields).map(function (key) {
    var field = fields[key] || {};
    if (typeof field.message === "string" && field.message.trim()) return field.message;
    return field.code === "INCORRECT_SOLUTION"
      ? "Ответ неверный. Проверьте фотографию и рамки, затем выберите другой вариант."
      : "Ответ не принят. Проверьте выбранный вариант и попробуйте ещё раз.";
  });
  this.setValidationMessage(messages.join("\n") || "Ответ не принят. Проверьте выбранный вариант и попробуйте ещё раз.");
};
ReviewTask.prototype.validate = function () {
  var value = (this.getSolution() || {}).output_values || {};
  var message = "";
  if (!value.verdict) message = "Выберите ответ";
  else if (["OK", "MISSING", "EXTRA", "BOUNDARY", "LOAD_ERROR"].indexOf(value.verdict) === -1) message = "Выберите один из предложенных ответов";
  else if (value.verdict !== "LOAD_ERROR" && this.imageState !== "loaded") message = "Дождитесь загрузки фотографии. Если она не открывается, выберите «Ошибка загрузки».";
  else if (value.verdict === "LOAD_ERROR" && this.imageState === "loaded") message = "Фотография загрузилась. Проверьте её и выберите оценку разметки.";
  var error = message
    ? {task_id: this.getTask().id, errors: {verdict: {code: "INVALID", message: message}}}
    : TolokaTask.prototype.validate.apply(this, arguments);
  if (error) this.onValidationFail(error);
  else this.setValidationMessage("");
  return error;
};
ReviewTask.prototype.onDestroy = function () {
  var root = this.getDOMElement();
  var image = root.querySelector(".review-photo");
  image.onload = image.onerror = null;
  root.querySelectorAll("button, .review-verdicts input").forEach(function (element) {
    element.onclick = element.onchange = null;
  });
  if (this.practiceDestroy) this.practiceDestroy();
  if (reviewPracticeOwner === this) reviewPracticeOwner = null;
};
exports.Assignment = ReviewAssignment;
exports.Task = ReviewTask;
