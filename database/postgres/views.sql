-- PostgreSQL views (names lowercase; app remaps column case)

CREATE OR REPLACE VIEW vw_clientoutstanding AS
SELECT
    c.clientid,
    c.clientname,
    c.gstin,
    COALESCE(tax.totaltaxinvoiced, 0) AS totaltaxinvoiced,
    COALESCE(ng.totalnongstbilled, 0) AS totalnongstbilled,
    COALESCE(tax.totalgstinvoiced, 0) AS totalgstinvoiced,
    COALESCE(tax.totaltaxinvoiced, 0) + COALESCE(ng.totalnongstbilled, 0)
        + CASE WHEN c.openingbalance > 0 THEN c.openingbalance ELSE 0 END AS totalinvoiced,
    COALESCE(rcpt.totalcashreceived, 0)
        + CASE WHEN c.openingbalance < 0 THEN ABS(c.openingbalance) ELSE 0 END AS totalreceived,
    COALESCE(rcpt.totaltdsdeducted, 0) AS totaltdsdeducted,
    COALESCE(rcpt.totalgstpaid, 0) AS totalgstpaid,
    COALESCE(rcpt.totalcashreceived, 0) + COALESCE(rcpt.totaltdsdeducted, 0)
        + CASE WHEN c.openingbalance < 0 THEN ABS(c.openingbalance) ELSE 0 END AS effectivereceived,
    (COALESCE(tax.totaltaxinvoiced, 0) + COALESCE(ng.totalnongstbilled, 0)
        + CASE WHEN c.openingbalance > 0 THEN c.openingbalance ELSE 0 END)
        - (COALESCE(rcpt.totalcashreceived, 0) + COALESCE(rcpt.totaltdsdeducted, 0)
        + CASE WHEN c.openingbalance < 0 THEN ABS(c.openingbalance) ELSE 0 END) AS outstanding,
    COALESCE(tax.totalgstinvoiced, 0) - COALESCE(rcpt.totalgstpaid, 0) AS gstoutstanding,
    tax.lasttaxinvoicedate AS lastinvoicedate,
    ng.lastnongstdate AS lastnongstdate,
    rcpt.lastpaymentdate AS lastpaymentdate
FROM clientmaster c
LEFT JOIN (
    SELECT clientid,
           SUM(totalamount) AS totaltaxinvoiced,
           SUM(cgstamount + sgstamount + igstamount) AS totalgstinvoiced,
           MAX(invoicedate) AS lasttaxinvoicedate
    FROM taxinvoices
    WHERE COALESCE(invoicetype, 'TAX') <> 'PROFORMA'
    GROUP BY clientid
) tax ON tax.clientid = c.clientid
LEFT JOIN (
    SELECT clientid, SUM(amount) AS totalnongstbilled, MAX(billdate) AS lastnongstdate
    FROM nongstbills GROUP BY clientid
) ng ON ng.clientid = c.clientid
LEFT JOIN (
    SELECT clientid,
           SUM(amountreceived) AS totalcashreceived,
           SUM(tdsamount) AS totaltdsdeducted,
           SUM(gstpaidamount) AS totalgstpaid,
           MAX(receiptdate) AS lastpaymentdate
    FROM receipts GROUP BY clientid
) rcpt ON rcpt.clientid = c.clientid
WHERE c.isactive = 1;

CREATE OR REPLACE VIEW vw_outstandingdashboard AS
SELECT
    SUM(totalinvoiced) AS totalinvoiced,
    SUM(totalreceived) AS totalreceived,
    SUM(totaltdsdeducted) AS totaltdsdeducted,
    SUM(effectivereceived) AS totaleffectivereceived,
    SUM(outstanding) AS totaloutstanding,
    SUM(gstoutstanding) AS totalgstoutstanding,
    SUM(CASE WHEN outstanding > 0 THEN 1 ELSE 0 END) AS outstandingclientcount
FROM vw_clientoutstanding;

CREATE OR REPLACE VIEW vw_clientledger AS
SELECT
    c.clientid,
    DATE '1900-01-01' AS txndate,
    'OPEN' AS voucherno,
    'Opening Balance' AS particulars,
    CASE WHEN c.openingbalance > 0 THEN c.openingbalance ELSE 0 END AS debit,
    CASE WHEN c.openingbalance < 0 THEN ABS(c.openingbalance) ELSE 0 END AS credit,
    0 AS sortorder,
    'open' AS entrytype
FROM clientmaster c
WHERE c.openingbalance <> 0
UNION ALL
SELECT i.clientid, i.invoicedate, i.invoicenumber,
    'Tax Invoice ' || i.invoicenumber, i.totalamount, 0, 1, 'tax'
FROM taxinvoices i
WHERE COALESCE(i.invoicetype, 'TAX') <> 'PROFORMA'
UNION ALL
SELECT n.clientid, n.billdate, n.billnumber,
    'Non GST Bill ' || n.billnumber || ' — ' || LEFT(n.description, 80),
    n.amount, 0, 1, 'nongst'
FROM nongstbills n
UNION ALL
SELECT r.clientid, r.receiptdate, r.receiptnumber,
    'Receipt ' || r.receiptnumber
        || CASE WHEN r.paymentmode IS NOT NULL THEN ' (' || r.paymentmode || ')' ELSE '' END,
    0, r.amountreceived, 2, 'receipt'
FROM receipts r
UNION ALL
SELECT r.clientid, r.receiptdate, r.receiptnumber,
    'TDS deducted — ' || r.receiptnumber, 0, r.tdsamount, 2, 'tds'
FROM receipts r WHERE r.tdsamount > 0;

CREATE OR REPLACE VIEW vw_invoiceageing AS
SELECT
    i.clientid, c.clientname, i.invoiceid, i.invoicenumber, i.invoicedate, i.totalamount,
    COALESCE(alloc.paidagainstinvoice, 0) AS paidamount,
    i.totalamount - COALESCE(alloc.paidagainstinvoice, 0) AS pendingamount,
    (CURRENT_DATE - i.invoicedate) AS agedays,
    CASE
        WHEN (CURRENT_DATE - i.invoicedate) <= 30 THEN '0-30 Days'
        WHEN (CURRENT_DATE - i.invoicedate) <= 60 THEN '31-60 Days'
        WHEN (CURRENT_DATE - i.invoicedate) <= 90 THEN '61-90 Days'
        ELSE 'Above 90 Days'
    END AS agebucket
FROM taxinvoices i
INNER JOIN clientmaster c ON c.clientid = i.clientid
LEFT JOIN (
    SELECT i2.invoiceid,
           CASE
               WHEN client_rcpt.totalreceived >= client_inv.cumtotal THEN i2.totalamount
               WHEN client_rcpt.totalreceived <= client_inv.cumtotal - i2.totalamount THEN 0
               ELSE client_rcpt.totalreceived - (client_inv.cumtotal - i2.totalamount)
           END AS paidagainstinvoice
    FROM taxinvoices i2
    INNER JOIN (
        SELECT invoiceid, clientid, totalamount,
               SUM(totalamount) OVER (PARTITION BY clientid ORDER BY invoicedate, invoiceid) AS cumtotal
        FROM taxinvoices
        WHERE COALESCE(invoicetype, 'TAX') <> 'PROFORMA'
    ) client_inv ON client_inv.invoiceid = i2.invoiceid
    INNER JOIN (
        SELECT clientid, SUM(amountreceived) AS totalreceived FROM receipts GROUP BY clientid
    ) client_rcpt ON client_rcpt.clientid = i2.clientid
) alloc ON alloc.invoiceid = i.invoiceid
WHERE COALESCE(i.invoicetype, 'TAX') <> 'PROFORMA'
  AND i.totalamount - COALESCE(alloc.paidagainstinvoice, 0) > 0.01;

CREATE OR REPLACE VIEW vw_executivedashboard AS
SELECT
    COALESCE((SELECT SUM(totalamount) FROM taxinvoices WHERE COALESCE(invoicetype,'TAX') <> 'PROFORMA'), 0) AS totalinvoiced,
    COALESCE((SELECT SUM(amountreceived) FROM receipts), 0) AS totalreceived,
    COALESCE((SELECT SUM(outstanding) FROM vw_clientoutstanding), 0) AS totaloutstanding,
    COALESCE((SELECT SUM(cgstamount+sgstamount+igstamount) FROM taxinvoices WHERE COALESCE(invoicetype,'TAX') <> 'PROFORMA'), 0) AS totalgstreceivedable,
    COALESCE((SELECT SUM(tdsamount) FROM receipts), 0) AS totaltdsdeducted,
    (SELECT COUNT(*) FROM clientmaster WHERE isactive = 1) AS totalactiveclients,
    COALESCE((SELECT SUM(totalamount) FROM expenses), 0) AS totalexpenses,
    COALESCE((SELECT SUM(totalamount) FROM taxinvoices WHERE COALESCE(invoicetype,'TAX') <> 'PROFORMA'), 0)
        - COALESCE((SELECT SUM(totalamount) FROM expenses), 0) AS totalprofit;

CREATE OR REPLACE VIEW vw_segmentrevenue AS
SELECT s.businesssegmentid, s.businesssegmentname,
       COALESCE(SUM(i.totalamount), 0) AS totalrevenue,
       COUNT(DISTINCT i.clientid) AS clientcount,
       COUNT(i.invoiceid) AS invoicecount
FROM businesssegments s
LEFT JOIN taxinvoices i ON i.businesssegmentid = s.businesssegmentid
    AND COALESCE(i.invoicetype,'TAX') <> 'PROFORMA'
WHERE s.isactive = 1
GROUP BY s.businesssegmentid, s.businesssegmentname, s.sortorder;

CREATE OR REPLACE VIEW vw_segmentcollections AS
SELECT s.businesssegmentid, s.businesssegmentname,
       COALESCE(SUM(a.allocatedamount), 0) AS totalcollected
FROM businesssegments s
LEFT JOIN receiptinvoiceallocations a ON a.businesssegmentid = s.businesssegmentid
WHERE s.isactive = 1
GROUP BY s.businesssegmentid, s.businesssegmentname;

CREATE OR REPLACE VIEW vw_segmentoutstanding AS
SELECT s.businesssegmentid, s.businesssegmentname,
       COALESCE(rev.totalrevenue, 0) AS totalrevenue,
       COALESCE(col.totalcollected, 0) AS totalcollected,
       COALESCE(rev.totalrevenue, 0) - COALESCE(col.totalcollected, 0) AS outstanding,
       COALESCE(rev.clientcount, 0) AS clientcount
FROM businesssegments s
LEFT JOIN vw_segmentrevenue rev ON rev.businesssegmentid = s.businesssegmentid
LEFT JOIN vw_segmentcollections col ON col.businesssegmentid = s.businesssegmentid
WHERE s.isactive = 1;

CREATE OR REPLACE VIEW vw_segmentexpenseallocated AS
SELECT e.expenseid, e.expensedate, e.expensecategoryid,
       ec.categoryname, e.expensedescription, e.vendorname,
       s.businesssegmentid, s.businesssegmentname,
       CASE WHEN e.allocationtype = 'segment' THEN e.totalamount ELSE esa.allocatedamount END AS allocatedamount
FROM expenses e
INNER JOIN expensecategories ec ON ec.expensecategoryid = e.expensecategoryid
LEFT JOIN expensesegmentallocations esa ON esa.expenseid = e.expenseid
LEFT JOIN businesssegments s ON s.businesssegmentid = COALESCE(esa.businesssegmentid, e.businesssegmentid)
WHERE s.businesssegmentid IS NOT NULL;

CREATE OR REPLACE VIEW vw_segmentprofitability AS
SELECT s.businesssegmentid, s.businesssegmentname,
       COALESCE(rev.totalrevenue, 0) AS revenue,
       COALESCE(exp.totalexpense, 0) AS expense,
       COALESCE(rev.totalrevenue, 0) - COALESCE(exp.totalexpense, 0) AS profit,
       CASE WHEN COALESCE(rev.totalrevenue, 0) > 0
            THEN ROUND((COALESCE(rev.totalrevenue, 0) - COALESCE(exp.totalexpense, 0)) * 100.0 / rev.totalrevenue, 2)
            ELSE 0 END AS profitpercent
FROM businesssegments s
LEFT JOIN vw_segmentrevenue rev ON rev.businesssegmentid = s.businesssegmentid
LEFT JOIN (
    SELECT businesssegmentid, SUM(allocatedamount) AS totalexpense
    FROM vw_segmentexpenseallocated GROUP BY businesssegmentid
) exp ON exp.businesssegmentid = s.businesssegmentid
WHERE s.isactive = 1;

CREATE OR REPLACE VIEW vw_segmentmonthlyrevenue AS
SELECT i.businesssegmentid, s.businesssegmentname,
       EXTRACT(YEAR FROM i.invoicedate)::INT AS periodyear,
       EXTRACT(MONTH FROM i.invoicedate)::INT AS periodmonth,
       SUM(i.totalamount) AS revenue
FROM taxinvoices i
INNER JOIN businesssegments s ON s.businesssegmentid = i.businesssegmentid
WHERE COALESCE(i.invoicetype,'TAX') <> 'PROFORMA'
GROUP BY i.businesssegmentid, s.businesssegmentname,
         EXTRACT(YEAR FROM i.invoicedate), EXTRACT(MONTH FROM i.invoicedate);

CREATE OR REPLACE VIEW vw_segmentmonthlyexpense AS
SELECT businesssegmentid, businesssegmentname,
       EXTRACT(YEAR FROM expensedate)::INT AS periodyear,
       EXTRACT(MONTH FROM expensedate)::INT AS periodmonth,
       SUM(allocatedamount) AS expense
FROM vw_segmentexpenseallocated
GROUP BY businesssegmentid, businesssegmentname,
         EXTRACT(YEAR FROM expensedate), EXTRACT(MONTH FROM expensedate);

CREATE OR REPLACE VIEW vw_gstreceivable AS
SELECT
    i.invoiceid, i.clientid, c.clientname, i.invoicenumber, i.invoicedate,
    EXTRACT(YEAR FROM i.invoicedate)::INT AS invyear,
    EXTRACT(MONTH FROM i.invoicedate)::INT AS invmonth,
    EXTRACT(QUARTER FROM i.invoicedate)::INT AS invquarter,
    i.taxableamount, i.cgstamount, i.sgstamount, i.igstamount,
    (i.cgstamount + i.sgstamount + i.igstamount) AS totalgst,
    ia.pendingamount,
    CASE WHEN ia.pendingamount > 0 THEN ia.pendingamount * i.cgstamount / NULLIF(i.totalamount,0) ELSE 0 END AS cgstoutstanding,
    CASE WHEN ia.pendingamount > 0 THEN ia.pendingamount * i.sgstamount / NULLIF(i.totalamount,0) ELSE 0 END AS sgstoutstanding,
    CASE WHEN ia.pendingamount > 0 THEN ia.pendingamount * i.igstamount / NULLIF(i.totalamount,0) ELSE 0 END AS igstoutstanding
FROM taxinvoices i
INNER JOIN clientmaster c ON c.clientid = i.clientid
LEFT JOIN vw_invoiceageing ia ON ia.invoiceid = i.invoiceid
WHERE COALESCE(i.invoicetype,'TAX') <> 'PROFORMA';

CREATE OR REPLACE VIEW vw_reminderdashboard AS
SELECT
    SUM(CASE WHEN daystodue = 0 AND outstandingamount > 0 THEN 1 ELSE 0 END) AS duetoday,
    SUM(CASE WHEN daystodue BETWEEN -7 AND -1 AND outstandingamount > 0 THEN 1 ELSE 0 END) AS duethisweek,
    SUM(CASE WHEN daystodue BETWEEN 1 AND 30 AND outstandingamount > 0 THEN 1 ELSE 0 END) AS overdue,
    SUM(CASE WHEN daystodue > 30 AND outstandingamount > 0 THEN 1 ELSE 0 END) AS criticaloverdue
FROM (
    SELECT i.invoiceid,
           (CURRENT_DATE - COALESCE(i.duedate, i.invoicedate + 30)) AS daystodue,
           ia.pendingamount AS outstandingamount
    FROM taxinvoices i
    INNER JOIN vw_invoiceageing ia ON ia.invoiceid = i.invoiceid
) x;
