// Merge into tailwind.config.js. Values mirror design-system/tokens.css.
module.exports = {
  theme: {
    extend: {
      fontFamily: { sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'] },
      fontSize: {
        label: ['13px', { lineHeight: '1.3', letterSpacing: '0.05em' }],
        tag: ['11.5px', { lineHeight: '17px' }],
        small: ['14px', { lineHeight: '1.4' }],
        body: ['15px', { lineHeight: '1.45' }],
        card: ['17px', { lineHeight: '1.35' }],
        h1: ['22px', { lineHeight: '1.25' }],
        kpi: ['24px', { lineHeight: '1.2' }],
        hero: ['44px', { lineHeight: '1' }],
      },
      colors: {
        ink: { DEFAULT: '#0C1222', 2: '#3A4152', 3: '#5A6273' },
        ground: '#F5F6F8',
        surface: { DEFAULT: '#FFFFFF', 2: '#FAFBFC', hover: '#F7F8FB' },
        line: { DEFAULT: '#E2E5EB', 2: '#EDEFF3', strong: '#CDD2DC' },
        brand: { DEFAULT: '#5E3BFF', text: '#4A2FE0', tint: '#EFEBFF', ink: '#3A22C9' },
        red: { DEFAULT: '#D92D20', fg: '#A8200F', bg: '#FDECEA', band: '#F4B4AD' },
        amber: { DEFAULT: '#E5A000', fg: '#6E4600', bg: '#FFF2CF', band: '#F6CF7E' },
        green: { DEFAULT: '#2E9E62', fg: '#11603B', bg: '#E3F4EA' },
        grey: { DEFAULT: '#D0D4DC', fg: '#434B5C', bg: '#EEF0F4' },
        range: '#B8C2D6',
        deadline: '#B42318',
      },
      borderRadius: { sm: '6px', card: '10px' },
      width: { sidebar: '212px' },
    },
  },
};
