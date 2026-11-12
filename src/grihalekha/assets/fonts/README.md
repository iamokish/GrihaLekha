# Bundled fonts

GrihaLekha embeds these fonts in its offline PDF templates. Both are redistributed under the SIL Open Font License 1.1; the complete notices are beside the font files.

Downloaded 6 November 2026 from the Google Fonts repository:

- `NotoSans.ttf`: `https://github.com/google/fonts/blob/main/ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf`
  - SHA-256: `bfb7bb691513f12e734dc346c03a03f784912432d7e3fa8e56efcf906fe86b3d`
- `NotoSansDevanagari.ttf`: `https://github.com/google/fonts/blob/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth,wght%5D.ttf`
  - SHA-256: `14ec4af41f27482216d1c2229f417ff9b1425e1babb014e57d1d40d03229853e`

The source downloads are variable TrueType fonts. The distributed `*-Regular.ttf` and `*-Bold.ttf` files are static instances generated from these sources using fontTools `instantiateVariableFont`, pinning `wdth=100` and `wght=400` or `wght=700`. Their family/style/PostScript metadata and weight flags identify the corresponding Regular or Bold instance. Static instances let Chromium embed named TrueType subsets instead of Type3 glyph outlines. The font license and copyright remain unchanged.

Keep all four static fonts and both license files in distributions. The renderer loads embedded data URLs, so it does not require network access or system-installed Hindi fonts. Original variable sources are not included in the application; the URLs and checksums above identify them for reproducible regeneration.
