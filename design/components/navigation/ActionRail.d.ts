/** The narrow right-hand rail of Tier A actions. */
export interface RailAction {
  id: string;
  label: string;
  /** Lucide icon name. */
  icon: string;
  tone?: "default" | "danger";
  /** Non-empty means disabled; the string is the tooltip explaining why. */
  disabledReason?: string;
  /** Two or three words shown inline when disabled, e.g. "select process". */
  disabledHint?: string;
}
export interface ActionRailGroup { title: string; actions: RailAction[] }
export interface ActionRailProps {
  groups?: ActionRailGroup[];
  onAction?: (id: string) => void;
  width?: string;
  style?: React.CSSProperties;
}
export declare function ActionRail(props: ActionRailProps): JSX.Element;
