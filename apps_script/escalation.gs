/**
 * CX Auto-Escalation  (Google Apps Script, bound to the Google Sheet)
 * Rule: resolution_tat_hours > 24  AND  csat_score <= 2  ->  CRITICAL_ESCALATION
 *
 * Setup: Extensions > Apps Script > paste > Save > reload sheet > menu "CX Automation" > "Install hourly trigger"
 * Sheet tab must be named as in CONFIG.SHEET and keep the original CSV headers.
 */
const CONFIG = {
  SHEET: 'cx_tickets_feedback',
  LOG_SHEET: 'Escalation_Log',
  FLAG_COL: 'escalation_flag',        // new column, original ticket_status is preserved
  OVERWRITE_STATUS: false,            // true => also write CRITICAL_ESCALATION into ticket_status
  FLAG_VALUE: 'CRITICAL_ESCALATION',
  TAT_THRESHOLD_H: 24,
  CSAT_THRESHOLD: 2,
  ALERT_EMAIL: '',                    // e.g. 'cx-leads@company.com'; blank = no email
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('CX Automation')
    .addItem('Run escalation scan now', 'escalateCriticalTickets')
    .addItem('Install hourly trigger', 'installTrigger')
    .addToUi();
}

function escalateCriticalTickets() {
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(CONFIG.SHEET);
  if (!sh) throw new Error('Sheet not found: ' + CONFIG.SHEET);

  const values = sh.getDataRange().getValues();
  const header = values[0].map(String);
  const col = name => header.indexOf(name);
  const iId = col('ticket_id'), iTat = col('resolution_tat_hours'),
        iCsat = col('csat_score'), iStatus = col('ticket_status'), iStore = col('crm_lead_id');
  if ([iId, iTat, iCsat, iStatus].some(i => i < 0)) throw new Error('Missing required column(s).');

  // ensure flag column exists
  let iFlag = col(CONFIG.FLAG_COL);
  if (iFlag < 0) {
    iFlag = header.length;
    sh.getRange(1, iFlag + 1).setValue(CONFIG.FLAG_COL);
  }

  const newlyFlagged = [];
  const flagUpdates = [];
  const statusUpdates = [];
  for (let r = 1; r < values.length; r++) {
    const row = values[r];
    const tat = Number(row[iTat]);
    const csatRaw = row[iCsat];
    const alreadyFlagged = String(row[iFlag] || '') === CONFIG.FLAG_VALUE;
    // blank CSAT (open tickets, no survey yet) is NOT eligible
    const eligible = csatRaw !== '' && csatRaw !== null &&
                     tat > CONFIG.TAT_THRESHOLD_H && Number(csatRaw) <= CONFIG.CSAT_THRESHOLD;
    const flag = (eligible || alreadyFlagged) ? CONFIG.FLAG_VALUE : (row[iFlag] || '');
    flagUpdates.push([flag]);
    statusUpdates.push([eligible && CONFIG.OVERWRITE_STATUS ? CONFIG.FLAG_VALUE : row[iStatus]]);
    if (eligible && !alreadyFlagged) {
      newlyFlagged.push([new Date(), row[iId], row[iStore], tat, csatRaw, row[iStatus]]);
    }
  }

  const n = values.length - 1;
  if (n > 0) {
    sh.getRange(2, iFlag + 1, n, 1).setValues(flagUpdates);            // batched write
    if (CONFIG.OVERWRITE_STATUS) sh.getRange(2, iStatus + 1, n, 1).setValues(statusUpdates);
  }

  if (newlyFlagged.length) {
    logEscalations_(ss, newlyFlagged);
    if (CONFIG.ALERT_EMAIL) {
      MailApp.sendEmail(CONFIG.ALERT_EMAIL,
        `[CX] ${newlyFlagged.length} new CRITICAL_ESCALATION ticket(s)`,
        newlyFlagged.map(x => `${x[1]} | lead ${x[2]} | TAT ${x[3]}h | CSAT ${x[4]}`).join('\n'));
    }
  }
  SpreadsheetApp.getActive().toast(`${newlyFlagged.length} new critical escalation(s).`, 'CX Automation', 5);
}

function logEscalations_(ss, rows) {
  let log = ss.getSheetByName(CONFIG.LOG_SHEET);
  if (!log) {
    log = ss.insertSheet(CONFIG.LOG_SHEET);
    log.appendRow(['flagged_at', 'ticket_id', 'crm_lead_id', 'tat_hours', 'csat', 'status_before']);
  }
  log.getRange(log.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);
}

function installTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'escalateCriticalTickets')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('escalateCriticalTickets').timeBased().everyHours(1).create();
  SpreadsheetApp.getActive().toast('Hourly trigger installed.', 'CX Automation', 5);
}
