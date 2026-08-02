/* ===========================================================================
 * GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Every edit here will be silently destroyed the next time anyone runs
 *
 *     python3 tools/sync_config.py
 *
 * The source of truth is config/ in the git repository:
 *     rooms.json, eligibility.json, scoring.json, roster.csv
 * plus the floor-plan images referenced by rooms.json, which are inlined
 * below as data URIs so the web app needs no external requests.
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
  "schema_version": 1,
  "_comment": "Generated from the 2015-04-29 floor plan revision, then verifiable with tools/calibrate/. Coordinates are NORMALIZED (0-1) fractions of image_size, so they survive rescaling the image. Zone assignment follows the heavy divider drawn on the plan: desks 1-16 upper-years side, 17-28 first/second-year side. Desks 29-31 are on the upper-years half of the room; CONFIRM this is what the department intends before running.",
  "coord_space": "normalized",
  "zones": {
    "candidate_side": {
      "label": "Upper years side",
      "color": "#3d6fa8",
      "description": "Post-candidacy students. Left of the divider on the plan."
    },
    "precandidate_side": {
      "label": "First and second years side",
      "color": "#b0602f",
      "description": "Years 1-2, seated together for coursework."
    }
  },
  "rooms": [
    {
      "id": "main_office",
      "label": "Main Graduate Office (Room 406)",
      "image": "floorplans/main_office.png",
      "image_size": [
        1212,
        706
      ],
      "desks": [
        {
          "id": "D01",
          "label": "1",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.0487,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D02",
          "label": "2",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.0487,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D03",
          "label": "3",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1073,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D04",
          "label": "4",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1073,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D05",
          "label": "5",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1683,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D06",
          "label": "6",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1683,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D07",
          "label": "7",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2351,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D08",
          "label": "8",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2351,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D09",
          "label": "9",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2904,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D10",
          "label": "10",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2904,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D11",
          "label": "11",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.3482,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D12",
          "label": "12",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.3482,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D13",
          "label": "13",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4158,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D14",
          "label": "14",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4158,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D15",
          "label": "15",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4719,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D16",
          "label": "16",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.4719,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D17",
          "label": "17",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5355,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D18",
          "label": "18",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5355,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D19",
          "label": "19",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5974,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D20",
          "label": "20",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.5974,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D21",
          "label": "21",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.6576,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D22",
          "label": "22",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.6576,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D23",
          "label": "23",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7195,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D24",
          "label": "24",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7195,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D25",
          "label": "25",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7855,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D26",
          "label": "26",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.7855,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D27",
          "label": "27",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8416,
              0.2521,
              0.0545,
              0.1232
            ]
          }
        },
        {
          "id": "D28",
          "label": "28",
          "zone": "precandidate_side",
          "shape": {
            "rect": [
              0.8416,
              0.3754,
              0.0545,
              0.1204
            ]
          }
        },
        {
          "id": "D29",
          "label": "29",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.0561,
              0.5637,
              0.047,
              0.0765
            ]
          },
          "notes": "Lower-left wall, outside the two main rows."
        },
        {
          "id": "D30",
          "label": "30",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.1361,
              0.5637,
              0.0495,
              0.0765
            ]
          },
          "notes": "Lower-left wall, outside the two main rows."
        },
        {
          "id": "D31",
          "label": "31",
          "zone": "candidate_side",
          "shape": {
            "rect": [
              0.2211,
              0.5637,
              0.047,
              0.0765
            ]
          },
          "notes": "Lower-left wall, outside the two main rows."
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
    "Predicate forms for `when` (all keys ANDed):",
    "  scalar    {\"candidacy\": \"precandidate\"}        equality, case-insensitive",
    "  list      {\"year\": [1, 2]}                      membership",
    "  range     {\"year\": {\"min\": 1, \"max\": 2}}     inclusive; min/max optional",
    "  negation  {\"candidacy\": {\"not\": \"candidate\"}} inverts any of the above",
    "",
    "Attribute names must be columns in roster.csv. `allow_zones` is \"*\" or a",
    "list of zone ids defined in rooms.json.",
    "",
    "To add a rule (e.g. a new overflow room for 3rd years), insert it ABOVE the",
    "catch-all. Do not edit Python for this."
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
      "reason": "Pre-candidates (years 1-2) are seated together so they can work through coursework and quals as a cohort."
    },
    {
      "id": "everyone_else_anywhere",
      "when": {},
      "allow_zones": "*",
      "reason": "Post-candidacy students may sit anywhere in the department."
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
    "tie_break_seed: ANNOUNCE THIS ON DISCORD BEFORE THE FORM OPENS, then set",
    "seed_committed_at to when you announced it. The seed only ever chooses among",
    "assignments that are already exactly tied for optimal (see docs/SPEC.md 5.4),",
    "but publishing it first is what makes that claim checkable."
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
  "tie_break_seed": "REPLACE-ME-AND-ANNOUNCE-BEFORE-THE-FORM-OPENS",
  "seed_committed_at": null,
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

// Room id -> data URI for that room's floor plan, or null if the image
// file was missing when this was generated.
var FLOORPLAN_DATA_URI = {
  "main_office": null
};

var CONFIG_FINGERPRINT = "7d114a59ee96b761";
