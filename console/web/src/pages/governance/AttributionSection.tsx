import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUpRight, Database, Loader2, RefreshCw, UserRound, Braces, Radio } from 'lucide-react';
import { Button, cn } from '@foxl/ui';
import {
  getAttributionConfiguration, queryAttribution,
  type AttributionConfiguration, type AttributionEvidence,
} from '../../api';
import { StatCard, ErrorState, EmptyState, fmtNum } from '../../shared';

const WINDOWS = [1, 3, 24] as const;
const date = (seconds: number) => new Date(seconds * 1000).toLocaleString();
const tokens = (value: number | null) => value == null ? 'Not reported' : fmtNum(value);

/** Real CloudWatch evidence. Querying is an explicit action, never background polling. */
export function AttributionSection() {
  const [config, setConfig] = useState<AttributionConfiguration | null>(null);
  const [data, setData] = useState<AttributionEvidence | null>(null);
  const [windowHours, setWindowHours] = useState<number>(3);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    let active = true;
    getAttributionConfiguration()
      .then((value) => { if (active) setConfig(value); })
      .catch(() => { if (active) setError('Could not read the telemetry configuration. Reload the console and try again.'); });
    return () => { active = false; request.current?.abort(); };
  }, []);

  async function query() {
    if (loading) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    setData(null);
    try {
      const result = await queryAttribution(windowHours, controller.signal);
      if (!controller.signal.aborted) setData(result);
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'The query failed.');
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-border bg-card p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-2xl">
            <div className="mb-2 flex items-center gap-2 text-xs font-medium text-signal">
              <Database aria-hidden="true" className="size-4" />
              CloudWatch Logs Insights
            </div>
            <h2 className="text-xl font-semibold tracking-tight">Who is represented in the telemetry?</h2>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              Read the exported Claude Code request events, then compare the user labels
              before and after your Lab 3 change. This queries existing logs and starts no build.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex rounded-lg border border-border p-1" role="group" aria-label="Telemetry time window">
              {WINDOWS.map((hours) => (
                <button key={hours} type="button" disabled={loading} aria-pressed={hours === windowHours}
                  onClick={() => { setWindowHours(hours); setData(null); }}
                  className={cn('rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                    hours === windowHours ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted')}>
                  {hours}h
                </button>
              ))}
            </div>
            <Button onClick={query} disabled={loading || !config} className="gap-2">
              {loading ? <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" /> : <RefreshCw aria-hidden="true" className="size-4" />}
              {loading ? 'Querying…' : 'Query telemetry'}
            </Button>
          </div>
        </div>
        {config && (
          <dl className="mt-5 flex flex-wrap gap-x-7 gap-y-3 border-t border-border pt-4 text-xs">
            <div><dt className="mb-1 text-muted-foreground">Region</dt><dd className="font-mono">{config.region || 'Not configured'}</dd></div>
            <div className="min-w-0"><dt className="mb-1 text-muted-foreground">Log group</dt><dd className="break-all font-mono">{config.log_group}</dd></div>
            <div><dt className="mb-1 text-muted-foreground">Scope</dt><dd>Exported request events</dd></div>
          </dl>
        )}
      </section>

      {loading && <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
        Waiting for a complete CloudWatch result. Partial counts are withheld.
      </p>}
      {error && <ErrorState error={error} />}

      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard label="Request events" value={fmtNum(data.total_requests)} hint="Within the selected time window" />
            <StatCard accent label="Tagged requests" value={fmtNum(data.tagged_requests)}
              hint={data.coverage_percent == null ? 'No events to measure' : `${data.coverage_percent}% carry a user label`} />
            <StatCard label="Untagged requests" value={fmtNum(data.untagged_requests)} hint="An attribution gap to investigate" />
          </div>
          {data.rows.length === 0 ? (
            <EmptyState title="No matching request events"
              hint="Check the region, time window, and exporter. After a short Runtime prompt, allow about a minute for delivery and query again." />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border bg-card">
              <table className="w-full min-w-[600px] text-left text-sm">
                <caption className="border-b border-border px-5 py-4 text-left font-medium">
                  Exported usage by user label
                </caption>
                <thead className="bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th scope="col" className="px-5 py-3 font-medium">User label</th>
                    <th scope="col" className="px-5 py-3 text-right font-medium">Requests</th>
                    <th scope="col" className="px-5 py-3 text-right font-medium">Input tokens</th>
                    <th scope="col" className="px-5 py-3 text-right font-medium">Output tokens</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {data.rows.map((row) => (
                    <tr key={row.user == null ? 'missing-user' : `user:${row.user}`} className="hover:bg-muted/30">
                      <td className="break-all px-5 py-3.5">
                        {row.user ?? <span className="rounded-md bg-warning/10 px-2 py-1 text-xs font-medium text-warning">Untagged</span>}
                      </td>
                      <td className="px-5 py-3.5 text-right font-mono tabular-nums">{fmtNum(row.requests)}</td>
                      <td className="px-5 py-3.5 text-right font-mono tabular-nums">{tokens(row.input_tokens)}</td>
                      <td className="px-5 py-3.5 text-right font-mono tabular-nums">{tokens(row.output_tokens)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs leading-5 text-muted-foreground">
            <span>{date(data.start_time)} to {date(data.end_time)}</span>
            <span className="break-all">Query ID: <code>{data.query_id}</code></span>
          </div>
        </>
      )}

      {!data && !loading && !error && (
        <section className="grid gap-3 sm:grid-cols-3" aria-label="Attribution exercise">
          {[
            { icon: UserRound, title: 'Observe', text: 'Query the Lab 2 request events and find the untagged work.' },
            { icon: Braces, title: 'Connect', text: 'Implement and test the identity-to-telemetry mapping in Development.' },
            { icon: Radio, title: 'Verify', text: 'Run one short tagged prompt in Agents, then query the exported evidence.' },
          ].map(({ icon: Icon, title, text }, index) => (
            <div key={title} className="evidence-surface">
              <div className="mb-4 flex items-center justify-between">
                <Icon aria-hidden="true" className="size-5 text-signal" />
                <span className="font-mono text-xs text-muted-foreground">0{index + 1}</span>
              </div>
              <h3 className="text-sm font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">{text}</p>
            </div>
          ))}
        </section>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="evidence-surface">
          <h3 className="text-sm font-semibold">What this evidence proves</h3>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            A named row proves that a user label reached the collector and CloudWatch.
            Your manually supplied label does not prove a Cognito sign-in.
            Kiro usage, coordinator and review calls, and infrastructure billing are outside this query.
          </p>
          <div className="mt-4 flex flex-wrap gap-4 text-xs font-medium text-signal">
            <Link to="/development" className="inline-flex items-center gap-1">Open Development <ArrowUpRight aria-hidden="true" className="size-3.5" /></Link>
            <Link to="/agents" className="inline-flex items-center gap-1">Open Agents <ArrowUpRight aria-hidden="true" className="size-3.5" /></Link>
          </div>
        </section>
        {config && (
          <details className="evidence-surface">
            <summary className="text-sm font-semibold">View the exact query</summary>
            <p className="mt-3 text-xs leading-5 text-muted-foreground">
              The grouping key is the resource attribute user.id. You can run this same query in CloudWatch Logs Insights.
            </p>
            <pre className="mt-3 overflow-x-auto rounded-lg bg-muted/60 p-3 font-mono text-xs leading-6">{config.query}</pre>
          </details>
        )}
      </div>
    </div>
  );
}
