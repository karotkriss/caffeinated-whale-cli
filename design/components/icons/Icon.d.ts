/**
 * Lucide glyph rendered at the brand's 1.5px stroke weight, inheriting currentColor.
 */
export interface IconProps {
  /** Lucide icon name, e.g. "play", "rotate-cw", "terminal". See ICON_NAMES. */
  name: string;
  /** Pixel box. 14 in dense rows, 16 default, 18 in toolbars. */
  size?: number;
  strokeWidth?: number;
  className?: string;
  style?: React.CSSProperties;
}
export declare function Icon(props: IconProps): JSX.Element | null;
export declare const ICON_PATHS: Record<string, string>;
export declare const ICON_NAMES: string[];
