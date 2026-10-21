// What an uploaded file is, from its bytes (never from its name or the browser's content type).
// Accepted for the agents: PDF, DOCX, XLSX, HTML and plain text (services/marden/agents/text.py reads them).

export type UploadKind = 'pdf' | 'docx' | 'xlsx' | 'html' | 'txt';

export const CONTENT_TYPES: Readonly<Record<UploadKind, string>> = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  html: 'text/html',
  txt: 'text/plain',
};

const decoder = new TextDecoder('utf-8', { fatal: true });

function isUtf8Text(buf: Buffer): boolean {
  const head = buf.subarray(0, 65_536);
  if (head.includes(0)) return false;
  try {
    // A multi-byte character cut at the 64 KiB boundary is not an error: retry without the last 3 bytes.
    decoder.decode(head.length === 65_536 ? head.subarray(0, head.length - 3) : head);
    return true;
  } catch {
    return false;
  }
}

/** The kind of an uploaded file, or null when it is none of the accepted kinds. */
export function sniffUpload(buf: Buffer): UploadKind | null {
  if (buf.length === 0) return null;
  if (buf.subarray(0, 5).toString('latin1') === '%PDF-') return 'pdf';
  if (buf[0] === 0x50 && buf[1] === 0x4b) {
    // OOXML: the part names are stored uncompressed in the zip's local and central headers.
    const names = buf.toString('latin1');
    if (names.includes('word/document.xml')) return 'docx';
    if (names.includes('xl/workbook.xml')) return 'xlsx';
    return null;
  }
  if (!isUtf8Text(buf)) return null;
  const head = buf.subarray(0, 4096).toString('utf8').replace(/^\uFEFF/, '').trimStart().toLowerCase();
  if (head.startsWith('<!doctype html') || head.startsWith('<html') || (head.startsWith('<?xml') && head.includes('<html'))) {
    return 'html';
  }
  return 'txt';
}
