/** A monospace chip for a machine value: port, bench label, branch, site. */
export interface TagProps {
  /** Lucide icon name shown before the value. */
  icon?: string;
  /** Monospace by default because tags hold values a machine produced. */
  mono?: boolean;
  /** When given, renders a dismiss affordance. */
  onRemove?: () => void;
  children?: React.ReactNode;
  style?: React.CSSProperties;
}
export declare function Tag(props: TagProps): JSX.Element;
