// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Live Obsidian Lesson Log formatter and exporter.
 * Emits native Obsidian Markdown with callouts, inline LaTeX, and visual embeds.
 */

export class LessonLog {
  constructor() {
    this.entries = [];
    this.title = "Lesson";
    this.topic = "General";
    this.date = new Date().toISOString().slice(0, 10);
  }

  setMeta(title, topic) {
    if (title) this.title = title;
    if (topic) this.topic = topic;
  }

  addMessage(role, content) {
    if (!content) return;
    this.entries.push({ type: "message", role, content, time: Date.now() });
  }

  addQuiz(question, selectedLabel, isCorrect, isDontKnow, explanation) {
    this.entries.push({
      type: "quiz",
      question,
      selectedLabel,
      isCorrect,
      isDontKnow,
      explanation,
      time: Date.now(),
    });
  }

  addVisual(slug, kind, pngUrl) {
    this.entries.push({
      type: "visual",
      slug,
      kind,
      pngUrl,
      time: Date.now(),
    });
  }

  toMarkdown() {
    const lines = [
      `# ${this.title}`,
      `> Topic: **${this.topic}** | Date: \`${this.date}\``,
      "",
      "---",
      "",
    ];

    for (const item of this.entries) {
      if (item.type === "message") {
        if (item.role === "user") {
          lines.push(`### Learner\n${item.content}\n`);
        } else if (item.role === "assistant") {
          lines.push(`${item.content}\n`);
        }
      } else if (item.type === "quiz") {
        const gradeTag = item.isDontKnow
          ? "> [!warning] Checked: I don't know\n"
          : item.isCorrect
          ? "> [!success] Calibrated Check: Passed ✓\n"
          : "> [!failure] Calibrated Check: Needs Review ✗\n";

        lines.push(
          `> [!question] Check: ${item.question}\n` +
          `> - **Selected**: ${item.selectedLabel || "None"}\n>\n` +
          gradeTag +
          `> ${item.explanation || ""}\n`
        );
      } else if (item.type === "visual") {
        lines.push(`\n![[${item.slug}.png|550]]\n*Visual: ${item.slug} (${item.kind})*\n`);
      }
    }

    return lines.join("\n");
  }

  copyToClipboard() {
    const md = this.toMarkdown();
    return navigator.clipboard.writeText(md);
  }

  download() {
    const md = this.toMarkdown();
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const safeName = this.title.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
    a.href = url;
    a.download = `${safeName || "lesson"}-${this.date}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
}
