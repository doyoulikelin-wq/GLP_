import React from "react";
import { DataComponent, DataTable, ReportSection, RichNarrative, useDataApp } from "../../data-app-public.jsx";

/** Evidence-backed Chinese report; only reviewed snapshot rows are displayed. */
export function ReportContent() {
  const { snapshot, appTitle, visible, canEdit, mode, setAppTitle } = useDataApp();
  const rows = (id) => snapshot.queries[id]?.rows ?? [];
  const sections = snapshot.report?.sections ?? [];
  const table = (id) => {
    const query = snapshot.queries[id];
    if (!query?.displayTable || !visible(id + "-table")) return null;
    return <DataComponent id={id + "-table"} title={query.displayTable.title}
      queryId={id} kind="table" sourceRows={query.rows} displayRows={query.rows}>
      <DataTable rows={query.rows} columns={query.displayTable.columns}
        searchable={false} caption={query.displayTable.title} />
    </DataComponent>;
  };
  return <article className="report-content" aria-label="VHH 四项问题处理报告">
    <header className="report-hero">
      <h1 data-data-app-title contentEditable={canEdit && mode === "edit"} suppressContentEditableWarning
        onBlur={canEdit && mode === "edit" ? (event) => setAppTitle(event.currentTarget.textContent.trim() || appTitle) : undefined}>{appTitle}</h1>
    </header>
    {sections.map((section) => <React.Fragment key={section.id}>
      {visible(section.id) && <ReportSection id={section.id} title={section.title}
        queryId={section.queryId} sourceRows={rows(section.queryId)} showHeading={false}>
        <RichNarrative id={section.id + ":body"} value={"## " + section.title + "\n\n" + section.text} />
      </ReportSection>}
      {section.tableQuery && table(section.tableQuery)}
    </React.Fragment>)}
  </article>;
}
