/** Presentation only: persisted workspace keys and service identifiers stay stable. */
export const BRAND_NAME = 'DataPilot';
export const BRAND_ICON = '/brand/datapilot-icon.png';

export function BrandIcon({ decorative = true }: { decorative?: boolean }) {
  return <img className="brand-icon" src={BRAND_ICON} alt={decorative ? '' : BRAND_NAME}
    aria-hidden={decorative ? true : undefined} width={64} height={64} draggable={false} />;
}

export function BrandWordmark() {
  return <strong className="brand-wordmark">Data<span>Pilot</span></strong>;
}
