const lessons = [
  "Install VS Code",
  "Install Git",
  "First commit and push",
  "Install Python",
  "Install Node and Claude Code",
  "Build the HTML page",
  "Style the HTML page"
];

const list = document.getElementById("lessons");
const countEl = document.getElementById("count");
const totalEl = document.getElementById("total");
const bar = document.getElementById("bar");

function updateProgress() {
  const boxes = document.querySelectorAll("#lessons input");
  let done = 0;
  boxes.forEach(function (box) {
    if (box.checked) {
      done = done + 1;
    }
  });
  countEl.textContent = done;
  bar.value = done;
}

lessons.forEach(function (name) {
  const item = document.createElement("li");
  const label = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.addEventListener("change", updateProgress);
  label.appendChild(box);
  label.append(" " + name);
  item.appendChild(label);
  list.appendChild(item);
});

totalEl.textContent = lessons.length;
bar.max = lessons.length;
updateProgress();
