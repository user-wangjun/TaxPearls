"use strict";
// Presentation only: the original structured source coordinates remain intact.
function formatPolicySourceLocation(value) {
  if (!value) return "原文位置待核对";
  let source = value;
  if (typeof source === "string") {
    try { source = JSON.parse(source); }
    catch { return source; }
  }
  if (!source || typeof source !== "object" || Array.isArray(source)) return "原文位置待核对";
  const file = source.text_path || source.raw_path || source.retained_text_path || source.retained_raw_path;
  const parts = typeof file === "string" ? [file.replace(/\\/g, "/").split("/").pop()] : [];
  const kind = source.provenance_kind;
  if (kind === "catalog_annotation") parts.push("政策目录说明", source.catalog_field?.match(/\[id=([^\]]+)\]/)?.[1]);
  else if (kind === "manifest_annotation") parts.push("版本与适用范围整理", source.manifest_document_id);
  else if (kind === "retained_catalog_snapshot") parts.push("已采原文快照", source.snapshot_field?.match(/\[id=([^\]]+)\]/)?.[1]);
  else {
    const pages = Array.isArray(source.pdf_pages) ? source.pdf_pages.filter(p => Number.isInteger(p) && p > 0) : [];
    if (pages.length) parts.push("第 " + [...new Set(pages)].join("、") + " 页");
    if (Number.isInteger(source.start_line) && source.start_line > 0) {
      const end = Number.isInteger(source.end_line) && source.end_line > source.start_line ? "–" + source.end_line : "";
      parts.push("第 " + source.start_line + end + " 行");
    } else if (Number.isInteger(source.start_char) && source.start_char >= 0 &&
               Number.isInteger(source.end_char_exclusive) && source.end_char_exclusive > source.start_char) {
      parts.push("第 " + (source.start_char + 1) + "–" + source.end_char_exclusive + " 字符");
    }
  }
  return parts.filter(p => typeof p === "string" && p).join(" · ") || "原文位置待核对";
}
