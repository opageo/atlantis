Search in the ATBD document of VIIRS https://www.star.nesdis.noaa.gov/jpss/documents/ATBD/ATBD_VIIRS_Flood_Mapping_v1.0.pdf about the bare-land classification methodology and definition
**Response**
## Bare-land classification in the VIIRS Flood Mapping ATBD

The ATBD does **not provide a standalone ecological or land-cover definition of “bare land.”** Instead, bare land is an **operational spectral class** used by the flood-detection algorithm to distinguish non-vegetated dry surfaces from vegetation, water, cloud, snow/ice, and shadows.

### 1. Operational definition

Within the VFM processing chain, a **clear-sky bare-land pixel** is a cloud-free and snow/ice-free land pixel whose VIIRS spectral and thermal characteristics are classified as bare land rather than vegetation, water, or cloud. In the final `WaterDetection` output, it is represented by:

* **Value 16: Clear-sky bare land**
* **Value 17: Clear-sky vegetation**
* Collectively, these belong to the broader category of **clear-sky land**, which the ATBD describes as including vegetation and bare soil.

Thus, “bare land” in this product should be understood as an **algorithmic dry-land category**, not necessarily permanently barren terrain. Depending on the scene, it can include desert, exposed soil, sparsely vegetated land, urban-like surfaces, burned areas, volcanic terrain, and other surfaces that spectrally resemble bare soil. The ATBD explicitly discusses desert, urban land, dark volcanic land, burn scars, cropland, grassland, and shrubland as conditions that can affect the water-versus-bare-land decision.

***

## 2. Physical basis for distinguishing bare land

The main physical distinction is based on reflectance in three VIIRS imaging bands:

* **I1 visible:** approximately 0.64 µm
* **I2 near infrared:** approximately 0.865 µm
* **I3 short-wave infrared:** approximately 1.61 µm

The ATBD states that bare-land reflectance generally **increases with wavelength**, reaching its maximum in the SWIR band. Water shows the opposite behavior: it normally has low reflectance in all three bands, with reflectance close to zero in SWIR when sun glint is absent. Vegetation is differentiated principally by its relatively high NIR reflectance.

Conceptually:

| Surface    |          VIS |      NIR |          SWIR |
| ---------- | -----------: | -------: | ------------: |
| Water      |          Low | Very low |     Near zero |
| Vegetation | Moderate/low |     High |      Moderate |
| Bare land  |     Moderate |   Higher | Often highest |

This spectral behavior is used together with the following indices:

$
NDVI=\frac{R_{NIR}-R_{VIS}}{R_{NIR}+R_{VIS}}
$

$
NDSI=\frac{R_{VIS}-R_{SWIR}}{R_{VIS}+R_{SWIR}}
$

$
NDWI=\frac{R_{NIR}-R_{SWIR}}{R_{NIR}+R_{SWIR}}
$

The ATBD emphasizes that no single reflectance band or index independently separates water, vegetation, and bare land under all conditions. Classification therefore uses the variables jointly.

***

## 3. Bare-land classification methodology

### Step 1: Cloud and snow/ice screening

The algorithm first performs a preliminary separation of pixels into:

1. cloud,
2. snow/ice, and
3. clear-sky land.

This screening uses VIS, NIR and SWIR reflectances, NDVI, NDSI, NDWI, and the I5 thermal brightness temperature, together with ancillary information such as land cover, land/sea mask, DEM, surface-temperature climatology, and albedo climatology. Mahalanobis-distance and threshold methods are used in this preliminary stage, with different handling for desert and non-desert land.

### Step 2: Sun-glint stratification

The clear-sky land pixels are then divided into:

* pixels without relevant sun-glint contamination, and
* pixels under minor-to-moderate sun-glint conditions.

This division uses a lookup table derived from the MERIS sun-glint flag approach. The distinction is important because sun glint can increase water reflectance, especially in NIR and SWIR, making water resemble bare land.

### Step 3: Decision-tree classification

The principal classifier is a **J48graft/J48 decision tree based on the C4.5 algorithm**. It classifies clear-sky land pixels into:

* water,
* mixed water,
* vegetation,
* bare land, and
* residual cloud.

The decision tree uses:

* VIS reflectance,
* NIR reflectance,
* SWIR reflectance,
* NDVI,
* NDSI,
* NDWI, and
* I5 brightness temperature.

The ATBD reports that approximately **600,000 training samples from more than 500 VIIRS granules** were collected across North America, Africa, Europe, Asia, and Australia. The training population contained four principal reference categories: water, bare land, vegetation, and cloud.

### Step 4: Land-cover-specific decision trees

Because bare land and vegetation differ among deserts, forests, urban areas, croplands, shrublands, and other surface types, a single global tree was found insufficient. A VIIRS global land-cover dataset, derived from AVHRR/IGBP land cover and updated using 2017 VIIRS surface-type data, is therefore used to select or support land-cover-specific classification.

The methodology is:

1. Apply a general J48graft tree to all clear-sky pixels.
2. Identify land-cover types with poor classification performance.
3. Collect additional training samples for those land-cover types.
4. Train specialized decision trees.
5. Apply the specialized trees under the associated land-cover and observational conditions.

The operational system contains **16 decision trees** for supra-vegetation/bare-land water detection under different conditions.

The ATBD’s example tree for discriminating water from bare land places the **SWIR reflectance at the root node**, indicating that SWIR has the greatest information-gain ratio in that example. NDSI and visible reflectance appear at lower levels of the tree and provide additional separation.

***

## 4. Post-classification checks affecting bare land

The initial decision-tree result is not treated as final because dark, moist, urban, volcanic, or sun-glint-affected surfaces may be confused with water.

### Dark volcanic land

If a known dark-volcanic-land pixel is classified as water, its NDWI must exceed **0.25**. Otherwise, it is reclassified as dry land. Remaining false-water detections can later be removed through topographic analysis.

### Moist land or vegetation

Moist high-latitude surfaces can be initially classified as water. The algorithm compares the candidate pixel with background values in a moving **200 × 200 pixel window**, using VIS, NIR, NDVI, NDSI, and I5 temperature. The pixel is retained as water only when the combined spectral and thermal conditions support a water interpretation.

### Large solar-zenith angles

For pixels classified as water where solar zenith exceeds 45° and NDVI is above zero, NIR and SWIR values are compared with surrounding dry land. The candidate is retained as water only when its NIR reflectance is at least about **6 percentage points lower** and its SWIR reflectance about **5 percentage points lower** than the background dry land. Otherwise, it is classified as dry land.

### Sun-glint-contaminated water

Water affected by moderate sun glint can be classified as bare land because SWIR reflectance increases. In regions flagged by the sun-glint lookup table, pixels initially classified as bare land are reconsidered using VIS, SWIR, NDSI, and NDVI thresholds. Pixels satisfying the specified combinations are reclassified as sun-glint-contaminated water.

### Urban land

Urban surfaces with aerosol, pollution, or thin snow can exhibit reduced SWIR and increased VIS/NIR signals, producing false water detections. The ATBD therefore applies sensor-angle-dependent NDSI thresholds, with stricter conditions in parts of the Northern Hemisphere between 60°E and 180°E.

***

## 5. Bare land in minor-water detection

After the main decision-tree classification, the VFM algorithm searches for lower-water-fraction pixels that the primary classifier may have left as vegetation or bare land.

For a candidate currently classified as bare land:

1. The algorithm searches around confirmed water pixels and mapped rivers, lakes, and reservoirs.
2. Only land pixels within approximately **30 m elevation difference** from a confirmed water pixel are considered.
3. The candidate’s VIS, NIR, SWIR, NDSI, and thermal characteristics are compared with nearby bare-land pixels of similar land-cover type.
4. Absolute upper limits of approximately **25% reflectance in VIS, NIR, and SWIR** are imposed.
5. Land-cover-dependent change thresholds determine whether the pixel remains bare land or is reclassified as minor water.

Different thresholds are specified for desert, urban land, grassland/open shrubland, cropland/closed shrubland, and other land-cover types. This indicates that “bare-land classification” is not based on one universal threshold, but on the pixel’s **spectral departure from an expected local dry-land background**.

***

## Concise interpretation

> **Bare land in the VIIRS VFM ATBD is a clear-sky, non-snow/ice land pixel classified as non-vegetated dry land through a C4.5/J48 decision-tree system using VIS, NIR, SWIR, spectral indices, and thermal information. The result is refined using land-cover-specific trees, sun-glint information, neighborhood comparisons, terrain information, and special checks for desert, volcanic, urban, moist, and other confusing surfaces.**

The most relevant parts of the ATBD are:

* **Section 2.3.1.1**, Physical basis for water detection, pages 18 to 21
* **Section 2.3.2.1.1**, Cloud and snow/ice masking, page 27
* **Section 2.3.2.1.3**, Supra-vegetation/bare-land water detection, pages 30 to 33
* **Section 2.3.2.5**, Change detection for minor water extraction, pages 39 to 41
* **Table 2-4**, output class definitions, page 43
