import { getRouteApi } from '@tanstack/react-router';
import { ExplorerPage } from './explorer-page';
import { KindTablePage } from './kind-table-page';
import { RecordPage } from './record-page';

const home = getRouteApi('/$domain/explorer/');
const kindTable = getRouteApi('/$domain/explorer/$kind/');
const record = getRouteApi('/$domain/explorer/$kind/$key');

/** The Explorer home bound to its URL state; loaded lazily by the route. */
export function ExplorerRoute() {
  const { q, kind } = home.useSearch();
  return <ExplorerPage q={q} kind={kind} />;
}

/** The records of one Kind bound to its URL state; loaded lazily by the route. */
export function KindTableRoute() {
  const { kind } = kindTable.useParams();
  const { sort, dir, cursor } = kindTable.useSearch();
  return <KindTablePage kind={kind} search={{ sort, dir, cursor }} />;
}

/** One record bound to its URL state; loaded lazily by the route. */
export function RecordRoute() {
  const { kind, key } = record.useParams();
  return <RecordPage kind={kind} recordKey={key} />;
}
