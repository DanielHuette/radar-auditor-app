import { PDFDocument, StandardFonts, rgb, type PDFFont, type PDFPage } from "pdf-lib";
import type { EstimateStatement } from "./costs";
import { formatDate, formatEuroPrecise, formatNumber } from "./format";

export interface EstimateDocument {
  customerName: string;
  requestedAt: string;
  verdict: string | null;
  statement: EstimateStatement;
}

const PAGE_WIDTH = 595;
const PAGE_HEIGHT = 842;
const MARGIN = 56;
const BODY_SIZE = 10;
const ROW_HEIGHT = 20;
const INK = rgb(0.016, 0.024, 0.043);
const MUTED = rgb(0.42, 0.416, 0.392);
const GOLD = rgb(0.663, 0.502, 0.227);
const RULE = rgb(0.85, 0.84, 0.8);
const NOT_IN_FONT = /[^\x20-\x7e\u00a0-\u00ff€–„“]/g;

const COLUMNS = [
  { title: "Dienst", x: MARGIN, right: false },
  { title: "Abfrage", x: 195, right: false },
  { title: "Abrufe", x: 425, right: true },
  { title: "Einzelpreis", x: 485, right: true },
  { title: "Summe", x: PAGE_WIDTH - MARGIN, right: true },
] as const;

interface Sheet {
  pdf: PDFDocument;
  page: PDFPage;
  regular: PDFFont;
  bold: PDFFont;
  y: number;
}

function printable(text: string): string {
  return text.replace(/\u202f/g, "\u00a0").replace(NOT_IN_FONT, "?");
}

function write(sheet: Sheet, text: string, x: number, options: { bold?: boolean; size?: number; right?: boolean; muted?: boolean } = {}): void {
  const font = options.bold === true ? sheet.bold : sheet.regular;
  const size = options.size ?? BODY_SIZE;
  const line = printable(text);
  const left = options.right === true ? x - font.widthOfTextAtSize(line, size) : x;
  sheet.page.drawText(line, { x: left, y: sheet.y, size, font, color: options.muted === true ? MUTED : INK });
}

function rule(sheet: Sheet, color = RULE): void {
  sheet.page.drawLine({ start: { x: MARGIN, y: sheet.y + 14 }, end: { x: PAGE_WIDTH - MARGIN, y: sheet.y + 14 }, thickness: 0.6, color });
}

function nextRow(sheet: Sheet, height = ROW_HEIGHT): void {
  sheet.y -= height;
  if (sheet.y >= MARGIN) return;
  sheet.page = sheet.pdf.addPage([PAGE_WIDTH, PAGE_HEIGHT]);
  sheet.y = PAGE_HEIGHT - MARGIN;
}

function row(sheet: Sheet, cells: readonly string[], bold = false): void {
  COLUMNS.forEach((column, index) => {
    write(sheet, cells[index] ?? "", column.x, { bold, right: column.right });
  });
  nextRow(sheet);
}

function head(sheet: Sheet, document: EstimateDocument): void {
  write(sheet, "RepoCity", MARGIN, { bold: true, size: 13 });
  nextRow(sheet, 44);
  write(sheet, "Kostenvoranschlag", MARGIN, { bold: true, size: 24 });
  nextRow(sheet, 26);
  write(sheet, `Sichtbarkeitsprüfung ${document.customerName}`.trim(), MARGIN, { size: 12 });
  nextRow(sheet, 18);
  write(sheet, `Prüfung vom ${formatDate(document.requestedAt)}`, MARGIN, { muted: true });
  nextRow(sheet, 34);
  if (document.verdict === null) return;
  write(sheet, document.verdict, MARGIN);
  nextRow(sheet, 30);
}

function paidTable(sheet: Sheet, statement: EstimateStatement): void {
  rule(sheet, GOLD);
  row(sheet, COLUMNS.map((column) => column.title), true);
  for (const line of statement.paid) {
    rule(sheet);
    row(sheet, [line.source, line.service, formatNumber(line.calls), formatEuroPrecise(line.unitPrice), formatEuroPrecise(line.total)]);
  }
  rule(sheet, INK);
  row(sheet, ["Voranschlag gesamt", "", "", "", formatEuroPrecise(statement.total)], true);
}

function plannedList(sheet: Sheet, statement: EstimateStatement): void {
  nextRow(sheet, 16);
  write(sheet, "Geplante Aufrufe ohne Eurobetrag", MARGIN, { bold: true });
  nextRow(sheet);
  for (const line of statement.planned) {
    rule(sheet);
    row(sheet, [line.source, line.service, formatNumber(line.calls), "", ""]);
  }
}

export async function estimatePdf(document: EstimateDocument): Promise<Uint8Array> {
  const pdf = await PDFDocument.create();
  pdf.setTitle(`Kostenvoranschlag ${document.customerName}`.trim());
  const sheet: Sheet = {
    pdf,
    page: pdf.addPage([PAGE_WIDTH, PAGE_HEIGHT]),
    regular: await pdf.embedFont(StandardFonts.Helvetica),
    bold: await pdf.embedFont(StandardFonts.HelveticaBold),
    y: PAGE_HEIGHT - MARGIN,
  };
  head(sheet, document);
  if (document.statement.paid.length > 0) paidTable(sheet, document.statement);
  if (document.statement.planned.length > 0) plannedList(sheet, document.statement);
  nextRow(sheet, 16);
  write(sheet, "Hochgerechnet vor dem Start aus den geplanten Abrufen und den Einzelpreisen der Dienste.", MARGIN, { muted: true, size: 9 });
  return pdf.save();
}
