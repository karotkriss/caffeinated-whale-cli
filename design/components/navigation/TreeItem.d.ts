/**
 * One row of the fleet tree.
 */
export interface TreeItemProps {
  label: React.ReactNode;
  /** 0 = instance, 1 = bench, 2 = process. */
  depth?: number;
  /** Lucide icon name. */
  icon?: string;
  /** Renders a trailing health dot. */
  status?: "running" | "online" | "degraded" | "offline" | "unknown";
  expandable?: boolean;
  expanded?: boolean;
  selected?: boolean;
  /** Small right-aligned monospace metadata, e.g. a port. */
  meta?: React.ReactNode;
  mono?: boolean;
  onToggle?: () => void;
  onClick?: () => void;
  style?: React.CSSProperties;
}
export declare function TreeItem(props: TreeItemProps): JSX.Element;
