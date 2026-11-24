// A connector's service mark, drawn from the bundled monochrome brand set so it takes the ink
// tone of wherever it sits. A custom server, or a service without a mark, wears the MCP one.

import { BRAND_MARKS } from "../lib/brandMarks";

export function ConnectorMark({ preset, size = 16 }: { preset: string | null | undefined; size?: number }) {
  const mark = (preset ? BRAND_MARKS[preset] : undefined) ?? BRAND_MARKS.mcp;
  if (!mark) return null;
  return (
    <svg
      className="brand-mark"
      viewBox={mark.viewBox}
      width={size}
      height={size}
      aria-hidden="true"
      focusable="false"
      dangerouslySetInnerHTML={{ __html: mark.body }}
    />
  );
}
