/** The pixel form of a Rich table. */
export interface DataColumn {
  key: string;
  header: string;
  /** CSS grid track, e.g. "160px" or "minmax(0,1fr)". */
  width?: string;
  align?: "left" | "right" | "center";
  /** Column tint, matching the CLI's Rich column styles. */
  tint?: "cyan" | "magenta" | "green" | "yellow" | "red" | "dim";
  /** Monospace by default; set false for prose columns. */
  mono?: boolean;
}
export interface DataTableProps {
  columns?: DataColumn[];
  rows?: Record<string, React.ReactNode>[];
  caption?: React.ReactNode;
  onRowClick?: (row: Record<string, React.ReactNode>, index: number) => void;
  selectedIndex?: number;
  style?: React.CSSProperties;
}
export declare function DataTable(props: DataTableProps): JSX.Element;
