// Typed access to the synthetic fixtures in apps/dashboard/fixtures/.
//
// JSON imports are inferred structurally (strings, not literal unions), so
// they are cast to the contract types here ONCE; src/contract.test.ts checks
// at runtime that they really conform. Tests should import from this module
// rather than casting fixture JSON themselves.
import approvalJson from '../../fixtures/approval-request.json'
import acmeJson from '../../fixtures/chart-ACME.json'
import novaJson from '../../fixtures/chart-NOVA.json'
import qbitJson from '../../fixtures/chart-QBIT-USD.json'
import historyJson from '../../fixtures/history.json'
import snapshotJson from '../../fixtures/snapshot.json'
import type { ApprovalRequest, ChartData, History, OptionsSleeve, Snapshot } from '../contract.ts'

export const snapshotFixture = snapshotJson as unknown as Snapshot
export const historyFixture = historyJson as unknown as History
export const approvalFixture = approvalJson as unknown as ApprovalRequest

/** Chart fixtures keyed by their R2 KEY (file name without "chart-" / ".json"). */
export const chartFixtures: Record<string, ChartData> = {
  ACME: acmeJson as unknown as ChartData,
  NOVA: novaJson as unknown as ChartData,
  'QBIT-USD': qbitJson as unknown as ChartData,
}

/** The snapshot's `as_of`, for freezing the clock in freshness tests. */
export const FIXTURE_AS_OF = snapshotFixture.as_of

/** The fixture's options sleeve (snapshot.options): one short ORBT put, live. */
export const optionsSleeveFixture = snapshotFixture.options as OptionsSleeve
