# Validation report — MeerKAT / 2MASS mosaic workflow

Comparison against `MeerKAT.ipynb` (run 2024-08-09 13:26:10, MontagePy 2.2.0).

| | |
|---|---|
| Verdict | **PASS** |
| Checks passed | 109 |
| Checks failed | 0 |
| Warnings (non-gating) | 0 |
| Checks skipped | 0 |

## Tier 1 — metadata assertions

| Check | Expected | Actual | Result |
|---|---|---|---|
| region centre longitude | `266.37081688220496` | `266.37081688220496` | pass |
| region centre latitude | `-29.22153052692637` | `-29.221530526926376` | pass |
| region size (deg) | `3.2165880120000003` | `3.2165880120000003` | pass |
| thumbnail width | `961` | `961` | pass |
| thumbnail height | `1000` | `1000` | pass |
| band H raw image count | `479` | `479` | pass |
| band H chunks reported | `>0` | `15` | pass |
| band H images in coadd | `>= 460 (notebook: None)` | `471` | pass |
| band H plane-fit failures within budget | `<= 25 (notebook J: None)` | `5` | pass |
| band H reprojection attrition | `>0` | `471 of 479` | pass |
| band J raw image count | `479` | `479` | pass |
| band J chunks reported | `>0` | `15` | pass |
| band J overlap pairs | `2133` | `2133` | pass |
| band J images in coadd | `>= 460 (notebook: 471)` | `471` | pass |
| band J plane-fit failures within budget | `<= 25 (notebook J: 6)` | `6` | pass |
| band J reprojection attrition | `>0` | `471 of 479` | pass |
| band K raw image count | `479` | `479` | pass |
| band K chunks reported | `>0` | `15` | pass |
| band K images in coadd | `>= 460 (notebook: None)` | `471` | pass |
| band K plane-fit failures within budget | `<= 25 (notebook J: None)` | `6` | pass |
| band K reprojection attrition | `>0` | `471 of 479` | pass |
| 2massh.fits dimensions | `10121x10527` | `10121x10527` | pass |
| 2massh.fits BUNIT inherited from template | `JY/BEAM` | `JY/BEAM` | pass |
| 2massh.fits CRVAL1 matches region.hdr | `266.366790598` | `266.366790598` | pass |
| 2massh.fits CRVAL2 matches region.hdr | `-29.2213778092` | `-29.2213778092` | pass |
| 2massh.fits CRPIX1 matches region.hdr | `5072.5` | `5072.5` | pass |
| 2massh.fits CRPIX2 matches region.hdr | `5265.5` | `5265.5` | pass |
| 2massh.fits CDELT1 matches region.hdr | `-0.000305556` | `-0.000305556` | pass |
| 2massh.fits CDELT2 matches region.hdr | `0.000305556` | `0.000305556` | pass |
| 2massh.fits CTYPE1 matches region.hdr | `RA---TAN` | `RA---TAN` | pass |
| 2massh.fits CTYPE2 matches region.hdr | `DEC--TAN` | `DEC--TAN` | pass |
| 2massj.fits dimensions | `10121x10527` | `10121x10527` | pass |
| 2massj.fits BUNIT inherited from template | `JY/BEAM` | `JY/BEAM` | pass |
| 2massj.fits CRVAL1 matches region.hdr | `266.366790598` | `266.366790598` | pass |
| 2massj.fits CRVAL2 matches region.hdr | `-29.2213778092` | `-29.2213778092` | pass |
| 2massj.fits CRPIX1 matches region.hdr | `5072.5` | `5072.5` | pass |
| 2massj.fits CRPIX2 matches region.hdr | `5265.5` | `5265.5` | pass |
| 2massj.fits CDELT1 matches region.hdr | `-0.000305556` | `-0.000305556` | pass |
| 2massj.fits CDELT2 matches region.hdr | `0.000305556` | `0.000305556` | pass |
| 2massj.fits CTYPE1 matches region.hdr | `RA---TAN` | `RA---TAN` | pass |
| 2massj.fits CTYPE2 matches region.hdr | `DEC--TAN` | `DEC--TAN` | pass |
| 2massk.fits dimensions | `10121x10527` | `10121x10527` | pass |
| 2massk.fits BUNIT inherited from template | `JY/BEAM` | `JY/BEAM` | pass |
| 2massk.fits CRVAL1 matches region.hdr | `266.366790598` | `266.366790598` | pass |
| 2massk.fits CRVAL2 matches region.hdr | `-29.2213778092` | `-29.2213778092` | pass |
| 2massk.fits CRPIX1 matches region.hdr | `5072.5` | `5072.5` | pass |
| 2massk.fits CRPIX2 matches region.hdr | `5265.5` | `5265.5` | pass |
| 2massk.fits CDELT1 matches region.hdr | `-0.000305556` | `-0.000305556` | pass |
| 2massk.fits CDELT2 matches region.hdr | `0.000305556` | `0.000305556` | pass |
| 2massk.fits CTYPE1 matches region.hdr | `RA---TAN` | `RA---TAN` | pass |
| 2massk.fits CTYPE2 matches region.hdr | `DEC--TAN` | `DEC--TAN` | pass |
| uncorrected.fits dimensions | `10121x10527` | `10121x10527` | pass |
| uncorrected.fits BUNIT inherited from template | `JY/BEAM` | `JY/BEAM` | pass |
| uncorrected.fits CRVAL1 matches region.hdr | `266.366790598` | `266.366790598` | pass |
| uncorrected.fits CRVAL2 matches region.hdr | `-29.2213778092` | `-29.2213778092` | pass |
| uncorrected.fits CRPIX1 matches region.hdr | `5072.5` | `5072.5` | pass |
| uncorrected.fits CRPIX2 matches region.hdr | `5265.5` | `5265.5` | pass |
| uncorrected.fits CDELT1 matches region.hdr | `-0.000305556` | `-0.000305556` | pass |
| uncorrected.fits CDELT2 matches region.hdr | `0.000305556` | `0.000305556` | pass |
| uncorrected.fits CTYPE1 matches region.hdr | `RA---TAN` | `RA---TAN` | pass |
| uncorrected.fits CTYPE2 matches region.hdr | `DEC--TAN` | `DEC--TAN` | pass |
| uncorrected data minimum | `-41.0209846496582` | `-41.0209846496582` | pass |
| uncorrected data maximum | `11176.1787109375` | `11176.1787109375` | pass |
| uncorrected stretch floor | `27.89344589703572` | `27.89344589703572` | pass |
| uncorrected stretch ceiling | `11176.1787109375` | `11176.1787109375` | pass |

## Tier 2 — notebook thumbnails

| Check | Expected | Actual | Result |
|---|---|---|---|
| thumbnail 2MASS_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail 2MASS_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail 2MASS_small.png PSNR (dB) | `>= 35.0` | `98.26` | pass |
| thumbnail 2MASS_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail 2massh_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail 2massh_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail 2massh_small.png PSNR (dB) | `>= 35.0` | `inf` | pass |
| thumbnail 2massh_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail 2massj_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail 2massj_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail 2massj_small.png PSNR (dB) | `>= 35.0` | `inf` | pass |
| thumbnail 2massj_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail 2massk_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail 2massk_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail 2massk_small.png PSNR (dB) | `>= 35.0` | `inf` | pass |
| thumbnail 2massk_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail MeerKAT_2MASS_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail MeerKAT_2MASS_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail MeerKAT_2MASS_small.png PSNR (dB) | `>= 35.0` | `98.93` | pass |
| thumbnail MeerKAT_2MASS_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail MeerKAT_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail MeerKAT_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail MeerKAT_small.png PSNR (dB) | `>= 35.0` | `inf` | pass |
| thumbnail MeerKAT_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |
| thumbnail uncorrected_small.png geometry | `(1000, 961, 3)` | `(1000, 961, 3)` | pass |
| thumbnail uncorrected_small.png SSIM | `>= 0.980` | `1.0` | pass |
| thumbnail uncorrected_small.png PSNR (dB) | `>= 35.0` | `inf` | pass |
| thumbnail uncorrected_small.png mean |Δ| per channel | `<= 2.00` | `0.0` | pass |

## Tier 3 — full-resolution pixels

| Check | Expected | Actual | Result |
|---|---|---|---|
| baseline 2massh.fits shape | `(10527, 10121)` | `(10527, 10121)` | pass |
| baseline 2massh.fits NaN mask | `0 differing pixels` | `0` | pass |
| baseline 2massh.fits max |Δ| | `<= 0.0103 (1e-06 x dynamic range 1.03e+04)` | `0` | pass |
| baseline 2massh.fits RMS Δ | `<= 0.0103` | `0` | pass |
| baseline 2massj.fits shape | `(10527, 10121)` | `(10527, 10121)` | pass |
| baseline 2massj.fits NaN mask | `0 differing pixels` | `0` | pass |
| baseline 2massj.fits max |Δ| | `<= 0.0112 (1e-06 x dynamic range 1.12e+04)` | `0` | pass |
| baseline 2massj.fits RMS Δ | `<= 0.0112` | `0` | pass |
| baseline 2massk.fits shape | `(10527, 10121)` | `(10527, 10121)` | pass |
| baseline 2massk.fits NaN mask | `0 differing pixels` | `0` | pass |
| baseline 2massk.fits max |Δ| | `<= 0.0107 (1e-06 x dynamic range 1.07e+04)` | `0` | pass |
| baseline 2massk.fits RMS Δ | `<= 0.0107` | `0` | pass |
| baseline uncorrected.fits shape | `(10527, 10121)` | `(10527, 10121)` | pass |
| baseline uncorrected.fits NaN mask | `0 differing pixels` | `0` | pass |
| baseline uncorrected.fits max |Δ| | `<= 0.0112 (1e-06 x dynamic range 1.12e+04)` | `0` | pass |
| baseline uncorrected.fits RMS Δ | `<= 0.0112` | `0` | pass |

