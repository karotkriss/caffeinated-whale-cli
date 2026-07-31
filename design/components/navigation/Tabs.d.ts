/** Underlined tabs for the detail pane. */
export interface TabItem { value: string; label: string; icon?: string; count?: number }
export interface TabsProps {
  items?: TabItem[];
  value?: string;
  onChange?: (value: string) => void;
  style?: React.CSSProperties;
}
export declare function Tabs(props: TabsProps): JSX.Element;
