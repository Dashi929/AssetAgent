/**
 * 校验报告。
 *
 * 这是"让美术信任 AI 产出"的关键界面，所以有两条硬要求：
 * 1. FAIL 项必须能点，点了要能高亮到 3D 视口的具体位置 —— 只说"UV 有重叠"没用。
 * 2. SKIPPED 必须显式展示并说明原因 —— 把"没测"伪装成"通过"是信任的自杀。
 */

import type { Locator, ValidationReport as Report } from '../api/types';
import { CheckBadge } from './StatusBadge';

interface Props {
  report: Report | null;
  onLocate?: (locator: Locator) => void;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

export function ValidationReport({ report, onLocate }: Props) {
  if (!report) {
    return <div className="empty">还没有校验报告。跑一次后处理管线就会生成。</div>;
  }

  const counts = report.results.reduce<Record<string, number>>((acc, item) => {
    acc[item.result] = (acc[item.result] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div>
      <div className="row wrap" style={{ marginBottom: 10 }}>
        <span className={`badge ${report.passed ? 'pass' : 'fail'}`}>
          {report.passed ? '校验通过' : '校验未通过'}
        </span>
        <span className="badge">PASS {counts.pass ?? 0}</span>
        {(counts.fail ?? 0) > 0 && <span className="badge fail">FAIL {counts.fail}</span>}
        {(counts.warn ?? 0) > 0 && <span className="badge warn">WARN {counts.warn}</span>}
        {(counts.skipped ?? 0) > 0 && <span className="badge skipped">SKIP {counts.skipped}</span>}
        <span className="spacer" />
        <span className="muted mono">规则集 {report.ruleset}</span>
      </div>

      <table className="rules">
        <thead>
          <tr>
            <th style={{ width: 78 }}>结果</th>
            <th style={{ width: 120 }}>规则</th>
            <th style={{ width: 96 }}>实测值</th>
            <th style={{ width: 96 }}>阈值</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {report.results.map((result) => {
            const locatable = result.locator.kind !== 'none' && result.locator.indices.length > 0;
            return (
              <tr
                key={result.rule}
                className={locatable ? 'clickable' : undefined}
                onClick={locatable ? () => onLocate?.(result.locator) : undefined}
                title={locatable ? '点击在 3D 视口中高亮问题位置' : undefined}
              >
                <td>
                  <CheckBadge result={result.result} />
                </td>
                <td>{result.label}</td>
                <td className="mono">{formatValue(result.value)}</td>
                <td className="mono">{formatValue(result.threshold)}</td>
                <td>
                  {result.message}
                  {locatable && (
                    <span className="muted"> · 点击定位（{result.locator.indices.length} 个面）</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {report.results.some((r) => r.result === 'skipped') && (
        <p className="muted" style={{ marginTop: 10 }}>
          标 SKIP 的规则表示"这次测不了"，不是"通过"。原因见各行说明；装齐依赖后重跑管线即可补上。
        </p>
      )}
    </div>
  );
}

export default ValidationReport;
