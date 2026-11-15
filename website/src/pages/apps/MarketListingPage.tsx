/**
 * MarketListingPage — one Kiro Space store listing, at
 * `/apps/-/market/:registry/:namespace/:name`.
 *
 * The `-/` prefix keeps the route off every installed app's name (`-` is not a
 * valid app name). Reads `GET /api/apps/market/listing`, uncached. The summary
 * renders as plain text; the listing's Markdown readme is not shown. Install
 * goes through InstallFromSpaceDialog, keyed by store and ref, never by name.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, Check, Star } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, ContentSkeleton } from '../../components/ui'
import AppIconTile from '../../components/appstore/AppIconTile'
import InstallFromSpaceDialog from '../../components/appstore/InstallFromSpaceDialog'
import { marketListingKey, readErrorKey } from '../../components/appstore/MarketListingRows'
import {
  MarketAccountChanged, readMarketListing, registriesReadError, useHiddenStores, useMarketRegistriesQuery, useMarketStatuses,
} from '../../components/appstore/marketAccount'
import { useMarketStatusText } from '../../components/appstore/MarketRegistryManager'
import { fmtNumber } from '../../i18n/format'

export default function MarketListingPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const statusText = useMarketStatusText()
  const { registry = '', namespace = '', name = '' } = useParams()
  const ref = `${namespace}/${name}`
  const [installing, setInstalling] = useState(false)
  const { data: registries, error: registriesError } = useMarketRegistriesQuery()
  const registriesFailed = registriesReadError(registriesError)
  const statuses = useMarketStatuses()
  // A held store, or one whose shared status is `unknown`: no listing, no
  // Install, only the status line, as in the store's block.
  const hidden = useHiddenStores().has(registry)
  const { data, error, isLoading } = useQuery({
    queryKey: marketListingKey(registry, ref),
    queryFn: () => readMarketListing(registry, ref),
    enabled: registries !== undefined,
  })
  const listing = !hidden && data?.status.state === 'signed_in' ? data.listing : null
  // One notice per cause: a sign-in problem shows the status line alone; a
  // read error while signed in (or while the status is unknown) shows the error.
  const signedOut = data?.status.state === 'needs_sign_in' || data?.status.state === 'no_access'
  const notSignedIn = !!data && data.status.state !== 'signed_in'
  const showError = !!data?.error && !signedOut
  const showStatus = signedOut || (notSignedIn && !data?.error)
  const readFailed = !!error && !(error instanceof MarketAccountChanged)

  return (
    <div className="max-w-[880px] mx-auto px-4 py-6" data-testid="market-listing-page">
      <Btn onClick={() => navigate('/apps')} className="mb-4">
        <ArrowLeft size={14} /> {t('components.appstore.marketStore.back')}
      </Btn>
      {registriesFailed && <ErrorNotice message={registriesFailed} askAgent />}
      {isLoading && !hidden && <ContentSkeleton rows={4} />}
      {/* A failed read shows its error even while the store is hidden: the
          status line alone would read "Checking sign-in…" for ever. */}
      {readFailed && <ErrorNotice message={(error as Error).message} askAgent />}
      {showError && data?.error && <ErrorNotice message={t(readErrorKey(data.error.code))} askAgent />}
      {hidden && !readFailed && !showError && (
        <p className="text-[12.5px] text-muted" data-testid="market-store-held">{statusText(statuses?.[registry])}</p>
      )}
      {!hidden && <>
      {showStatus && <ErrorNotice message={statusText(data?.status)} askAgent />}
      {data && !listing && !data.error && !notSignedIn && (
        <ErrorNotice message={t('components.appstore.marketStore.not_found')} askAgent />
      )}
      </>}
      {listing && (
        <div>
          {/* Narrow first: the icon, title and Install stack below `sm`, and a
              long unbroken title wraps inside its own column. */}
          <div className="flex flex-col sm:flex-row items-start gap-4">
            <AppIconTile name={listing.name} className="w-16 h-16 shrink-0" />
            <div className="flex-1 min-w-0 w-full">
              <h1 className="text-[22px] font-semibold text-text-strong [overflow-wrap:anywhere]">{listing.displayName}</h1>
              <div className="text-[13px] text-muted [overflow-wrap:anywhere]">
                {t('components.appstore.marketStore.publisher', { publisher: listing.publisher })}
                {listing.latestSemver && <> · {t('components.appstore.marketStore.version', { semver: listing.latestSemver })}</>}
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted mt-1">
                {listing.rating.status === 'ok' && (
                  <span className="inline-flex items-center gap-1" title={t('components.appstore.marketStore.rating_label')}>
                    <Star size={13} aria-hidden /> {fmtNumber(listing.rating.value, { maximumFractionDigits: 1 })}
                  </span>
                )}
                {listing.ratingCount.status === 'ok' && listing.ratingCount.value > 0 && (
                  <span>{t('components.appstore.marketStore.reviews', { n: fmtNumber(listing.ratingCount.value) })}</span>
                )}
                {listing.downloads.status === 'ok' && (
                  <span data-testid="market-downloads">{t('components.appstore.marketStore.downloads', { n: fmtNumber(listing.downloads.value) })}</span>
                )}
                {listing.downloads.status === 'unavailable' && (
                  <span data-testid="market-downloads">{t('components.appstore.marketStore.downloads_unavailable')}</span>
                )}
              </div>
            </div>
            <div className="shrink-0">
              {listing.installed ? (
                <span className="inline-flex items-center gap-1 text-[13px] text-muted"><Check size={13} /> {t('components.appstore.appListRow.installed')}</span>
              ) : (
                <Btn primary disabled={listing.nameTaken} onClick={() => setInstalling(true)}>
                  {t('components.appstore.appListRow.install')}
                </Btn>
              )}
            </div>
          </div>
          {listing.nameTaken && !listing.installed && (
            <p className="text-[12.5px] text-muted mt-2">{t('components.appstore.marketStore.name_taken')}</p>
          )}
          <p className="text-[14px] mt-5 whitespace-pre-wrap">{listing.summary}</p>
          {installing && (
            <InstallFromSpaceDialog
              row={listing}
              onClose={() => setInstalling(false)}
              onInstalled={() => {
                setInstalling(false)
                void queryClient.invalidateQueries({ queryKey: ['market-listing', registry] })
                void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
                window.dispatchEvent(new Event('mc:apps-changed'))
              }}
            />
          )}
        </div>
      )}
    </div>
  )
}
