/** A bounded log tail on the terminal surface. */
export interface LogLine {
  text: string;
  /** Chooses the terminal colour. Default "info". */
  level?: "error" | "warn" | "info" | "debug" | "success" | "command";
  /** Pre-formatted timestamp, dimmed inline. */
  time?: string;
}
export interface LogViewProps {
  lines?: LogLine[];
  showLineNumbers?: boolean;
  /** CSS height; the view scrolls inside it. */
  height?: number | string;
  style?: React.CSSProperties;
}
export declare function LogView(props: LogViewProps): JSX.Element;
