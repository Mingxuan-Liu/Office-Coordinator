/* ===========================================================================
 * GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Every edit here will be silently destroyed the next time anyone runs
 *
 *     python3 tools/sync_config.py
 *
 * The source of truth is config/ in the git repository:
 *     rooms.json, eligibility.json, scoring.json, roster.csv
 * No floor-plan bitmap is inlined unless a room asks for one with an "image"
 * key. rooms.json is a schematic -- the desk rectangles are the map -- so the
 * shipped config declares none and this file carries no image bytes.
 *
 * To change a desk, a zone, a rule, the scoring curve or the roster: edit the
 * file in config/, re-run the command above, and push both the config
 * change and this file in the same commit. CI runs `sync_config.py --check`
 * and fails if they disagree.
 *
 * CONFIG_FINGERPRINT below is a sha256 prefix over all of those inputs. It is
 * recorded in every submitted row (as part of client_version) so a response
 * can be tied back to the exact configuration that produced it.
 *
 * This file intentionally contains no generation timestamp: identical inputs
 * must produce a byte-identical file, or --check would be useless.
 * ======================================================================== */

var ROOMS_JSON = {
  "schema_version": 2,
  "_comment": [
    "SCHEMATIC, not a tracing. The floor-plan images are not drawn anywhere,",
    "so these rectangles are the map and their spacing has to carry the",
    "layout on its own:",
    "    narrow gap = two columns facing each other across a divider",
    "    wide gap   = a walking aisle",
    "    margin     = that column faces a wall",
    "Main office upper-years side: 1-2 and 15-16 face walls; the columns",
    "between them face each other in pairs. First/second-year side: 17-18",
    "and 27-28 face walls, the rest pair up.",
    "",
    "Coordinates are NORMALIZED (0-1). Rooms have no 'image': the form and",
    "the report both draw from these numbers alone, which is why there is no",
    "missing-image warning to ignore.",
    "",
    "`desks` are selectable. `features` are decoration and never clickable.",
    "The main office has none on purpose -- the tabs already say which room",
    "you are in, and the spacing says the rest.",
    "",
    "VERIFY BEFORE DEPLOYING: tools/calibrate/index.html, Import, Preview."
  ],
  "coord_space": "normalized",
  "zones": {
    "candidate_side": {
      "label": "Upper years side",
      "color": "#3d6fa8",
      "description": "Post-candidacy students, in the main office."
    },
    "precandidate_side": {
      "label": "First and second years side",
      "color": "#b0602f",
      "description": "Pre-candidates only, seated together for coursework."
    },
    "senior_office": {
      "label": "Senior office",
      "color": "#4a8b6f",
      "description": "Separate room. Post-candidacy students only."
    }
  },
  "rooms": [
    {
      "id": "main_office",
      "label": "Main Graduate Office (Room 406)",
      "image_size": [
        1334,
        318
      ],
      "features": [],
      "desks": [
        {
          "id": "D01",
          "label": "1",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.018,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D02",
          "label": "2",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.018,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D03",
          "label": "3",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.0945,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D04",
          "label": "4",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.0945,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D05",
          "label": "5",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1484,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D06",
          "label": "6",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1484,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D07",
          "label": "7",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2249,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D08",
          "label": "8",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2249,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D09",
          "label": "9",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2789,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D10",
          "label": "10",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2789,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D11",
          "label": "11",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.3553,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D12",
          "label": "12",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.3553,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D13",
          "label": "13",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4093,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D14",
          "label": "14",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4093,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D15",
          "label": "15",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4858,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D16",
          "label": "16",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4858,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D17",
          "label": "17",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5982,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D18",
          "label": "18",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5982,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D19",
          "label": "19",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.6747,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D20",
          "label": "20",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.6747,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D21",
          "label": "21",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7286,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D22",
          "label": "22",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7286,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D23",
          "label": "23",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8051,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D24",
          "label": "24",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8051,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D25",
          "label": "25",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8591,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D26",
          "label": "26",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8591,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D27",
          "label": "27",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.9355,
              0.1069,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D28",
          "label": "28",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.9355,
              0.3585,
              0.0465,
              0.2327
            ]
          }
        },
        {
          "id": "D29",
          "label": "29",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.018,
              0.7358,
              0.057,
              0.1887
            ]
          },
          "notes": "Lower-left wall, below the two main rows."
        },
        {
          "id": "D30",
          "label": "30",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.09,
              0.7358,
              0.057,
              0.1887
            ]
          },
          "notes": "Lower-left wall, below the two main rows."
        },
        {
          "id": "D31",
          "label": "31",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1619,
              0.7358,
              0.057,
              0.1887
            ]
          },
          "notes": "Lower-left wall, below the two main rows."
        }
      ]
    },
    {
      "id": "senior_office",
      "label": "Senior Office",
      "image_size": [
        531,
        400
      ],
      "features": [
        {
          "id": "senior_outline",
          "kind": "outline",
          "label": "Senior Office",
          "shape": {
            "rect": [
              0.2637,
              0.075,
              0.7156,
              0.875
            ]
          }
        },
        {
          "id": "senior_windows",
          "kind": "window",
          "label": "Windows",
          "shape": {
            "rect": [
              0.725,
              0.08,
              0.2505,
              0.035
            ]
          }
        },
        {
          "id": "senior_door_south",
          "kind": "door",
          "label": "Door",
          "shape": {
            "rect": [
              0.3107,
              0.795,
              0.113,
              0.135
            ]
          },
          "note": "Hinged on the south-east corner, so it opens the opposite way to the renderer's default. Change 'swing' to sw/nw/ne to move the hinge.",
          "swing": "se"
        }
      ],
      "desks": [
        {
          "id": "S01",
          "label": "1",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.3691,
              0.195,
              0.1168,
              0.175
            ]
          }
        },
        {
          "id": "S02",
          "label": "2",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.3691,
              0.445,
              0.1168,
              0.175
            ]
          }
        },
        {
          "id": "S03",
          "label": "3",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.6478,
              0.195,
              0.1168,
              0.175
            ]
          }
        },
        {
          "id": "S04",
          "label": "4",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.6478,
              0.445,
              0.1168,
              0.175
            ]
          }
        },
        {
          "id": "S05",
          "label": "5",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.8192,
              0.165,
              0.1563,
              0.24
            ]
          }
        },
        {
          "id": "S06",
          "label": "6",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.8192,
              0.4325,
              0.1563,
              0.2275
            ]
          }
        },
        {
          "id": "S07",
          "label": "7",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.8192,
              0.7075,
              0.1563,
              0.2275
            ]
          }
        },
        {
          "id": "S08",
          "label": "8",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.6328,
              0.74,
              0.1657,
              0.185
            ]
          }
        },
        {
          "id": "S09",
          "label": "9",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.4331,
              0.75,
              0.1507,
              0.1875
            ]
          }
        },
        {
          "id": "S10",
          "label": "10",
          "zone": "senior_office",
          "shape": {
            "rect": [
              0.4633,
              0.625,
              0.1996,
              0.1
            ]
          }
        }
      ]
    }
  ]
};

var ELIGIBILITY_JSON = {
  "schema_version": 1,
  "_comment": [
    "Rule table mapping roster attributes -> permitted zones. Evaluated top to",
    "bottom; the FIRST matching rule wins. The last rule must be a catch-all",
    "({\"when\": {}}) so nobody can fall through with undefined eligibility.",
    "",
    "Both cohorts are restricted, and neither can cross into the other's area:",
    "  pre-candidates  -> the first/second-year side only",
    "  everyone else   -> the upper-years side and the senior office",
    "Note the catch-all no longer says \"*\". Listing the zones explicitly is",
    "what keeps candidates out of the first/second-year side. If a new zone is",
    "added to rooms.json it will NOT become available to anyone until it is",
    "named here, which is the safer default.",
    "",
    "Predicate forms for `when` (all keys ANDed):",
    "  scalar    {\"candidacy\": \"precandidate\"}        equality, case-insensitive",
    "  list      {\"year\": [1, 2]}                      membership",
    "  range     {\"year\": {\"min\": 1, \"max\": 2}}     inclusive; min/max optional",
    "  negation  {\"candidacy\": {\"not\": \"candidate\"}} inverts any of the above",
    "",
    "Attribute names must be columns in roster.csv. The form only asks for",
    "candidacy, so a rule keyed on anything else reads the roster's value, which",
    "may be stale. To add a rule, insert it ABOVE the catch-all. Do not edit",
    "Python for this."
  ],
  "rules": [
    {
      "id": "precandidates_sit_together",
      "when": {
        "candidacy": "precandidate"
      },
      "allow_zones": [
        "precandidate_side"
      ],
      "reason": "Pre-candidates are seated together on the first and second years side, so they can work through coursework and quals as a cohort."
    },
    {
      "id": "candidates_upper_years_and_senior",
      "when": {},
      "allow_zones": [
        "candidate_side",
        "senior_office"
      ],
      "reason": "Post-candidacy students choose from the upper years side of the main office or the senior office. The first and second years side is kept for the pre-candidate cohort."
    }
  ]
};

var SCORING_JSON = {
  "schema_version": 1,
  "_comment": [
    "K is not declared anywhere. K = len(curves[primary_curve]). Every curve must",
    "have the same length. Changing K means editing these lists and nothing else.",
    "",
    "Curves must be strictly decreasing and strictly positive. A zero last rank",
    "would make 'got my 5th choice' worth the same as 'got nothing', which breaks",
    "the meaning of the K-floor.",
    "",
    "TIE-BREAK SEED",
    "  seed_year: \"auto\" means the tie-break seed is the calendar year of the run",
    "  -- 2026 for the 2026 cycle. It changes every year, and nobody chooses it,",
    "  so it cannot be shopped for a favourable outcome.",
    "",
    "  The year is resolved ONCE when the config loads and is written into",
    "  results.json. It is never read from the clock inside the solve. That is what",
    "  lets someone re-run the 2026 cycle in 2028 and still reproduce the published",
    "  hash. To re-run an old cycle deliberately, replace \"auto\" with that year:",
    "      \"seed_year\": 2026",
    "",
    "  The seed only ever chooses among assignments that are already exactly tied",
    "  for optimal (docs/SPEC.md 5.4). Setting seed_year makes tie_break_seed dead",
    "  config; the validator warns if both are present."
  ],
  "curves": {
    "linear_borda": [
      5,
      4,
      3,
      2,
      1
    ],
    "convex": [
      16,
      8,
      4,
      2,
      1
    ],
    "concave": [
      5,
      4.5,
      4,
      3.5,
      3
    ]
  },
  "primary_curve": "linear_borda",
  "comparison_curves": [
    "convex",
    "concave"
  ],
  "seed_year": "auto",
  "sensitivity_seeds": [
    "sensitivity-alpha",
    "sensitivity-beta",
    "sensitivity-gamma",
    "sensitivity-delta"
  ]
};

var ROSTER = [
  {
    "name": "Ada Lovelace",
    "email": "alovelace@umich.edu",
    "year": 5,
    "candidacy": "candidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Vera Rubin",
    "email": "vrubin@umich.edu",
    "year": 1,
    "candidacy": "precandidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Cecilia Payne",
    "email": "cpayne@umich.edu",
    "year": 2,
    "candidacy": "precandidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Jocelyn Bell",
    "email": "jbell@umich.edu",
    "year": 6,
    "candidacy": "candidate",
    "keeps_desk": "yes",
    "current_desk": "D07"
  },
  {
    "name": "Subrahmanyan Chandrasekhar",
    "email": "schandra@umich.edu",
    "year": 4,
    "candidacy": "candidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Annie Cannon",
    "email": "acannon@umich.edu",
    "year": 3,
    "candidacy": "candidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Henrietta Leavitt",
    "email": "hleavitt@umich.edu",
    "year": 1,
    "candidacy": "precandidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Fritz Zwicky",
    "email": "fzwicky@umich.edu",
    "year": 4,
    "candidacy": "candidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Nancy Roman",
    "email": "nroman@umich.edu",
    "year": 2,
    "candidacy": "precandidate",
    "keeps_desk": "no",
    "current_desk": ""
  },
  {
    "name": "Edwin Hubble",
    "email": "ehubble@umich.edu",
    "year": 3,
    "candidacy": "candidate",
    "keeps_desk": "no",
    "current_desk": ""
  }
];

// Room id -> data URI, or null if the declared image file was missing.
// Only rooms with an "image" key in rooms.json appear, so {} is the normal
// result: the desk rectangles are the map. Absent and null read alike.
var FLOORPLAN_DATA_URI = {};

var CONFIG_FINGERPRINT = "a774ab3f2e0bf6d9";
