/* Only public practice answers are normalized. Exam/work drawings stay untouched. */
function bboxTrainingMatches(answer, gold) {
  if (answer.status !== gold.status || answer.boxes.length !== gold.boxes.length) return false;
  if (gold.status === "no_butterflies") return true;
  if (gold.status !== "boxes") return false;
  function iou(a, b) {
    var w = Math.max(0, Math.min(a.left+a.width, b.left+b.width)-Math.max(a.left,b.left));
    var h = Math.max(0, Math.min(a.top+a.height, b.top+b.height)-Math.max(a.top,b.top));
    var intersection = w*h;
    return intersection / (a.width*a.height+b.width*b.height-intersection);
  }
  var matched = gold.boxes.map(function () { return -1; });
  function assign(i, seen) {
    for (var j=0; j<gold.boxes.length; j++) {
      if (seen[j] || iou(answer.boxes[i],gold.boxes[j]) < __TRAINING_IOU__) continue;
      seen[j] = true;
      if (matched[j] === -1 || assign(matched[j], seen)) { matched[j]=i; return true; }
    }
    return false;
  }
  return answer.boxes.every(function (_, i) { return assign(i, []); });
}
