/**
 * A fact row: a stable label and the value the tool actually measured.
 */
export interface KeyValueProps {
  label: React.ReactNode;
  /** null or "" renders an em dash — "not measured", never a zero. */
  value?: React.ReactNode;
  mono?: boolean;
  tone?: "default" | "muted" | "accent" | "danger";
  /** Small clarifier under the value, e.g. "never probed". */
  hint?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function KeyValue(props: KeyValueProps): JSX.Element;
